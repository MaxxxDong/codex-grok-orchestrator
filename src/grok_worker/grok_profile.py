"""Derived, plugin-free Grok home for lifecycle-managed workers."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE_MARKER = ".grok-worker-profile-v1"
PROFILE_LOCK = ".profile.lock"
WORKER_API_KEY_ENV = "GROK_WORKER_API_KEY"
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class GrokProfileError(RuntimeError):
    """The isolated Grok profile could not be prepared or verified."""


@dataclass(frozen=True)
class PreparedGrokProfile:
    home: Path
    runtime_home: Path
    source_home: Path
    environment: dict[str, str]


def source_grok_home(environ: Mapping[str, str] = os.environ) -> Path:
    explicit = environ.get("GROK_WORKER_SOURCE_GROK_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    configured = environ.get("GROK_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".grok").resolve()


def worker_runtime_home(environ: Mapping[str, str] = os.environ) -> Path:
    explicit = environ.get("GROK_WORKER_RUNTIME_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    legacy = environ.get("GROK_WORKER_GROK_HOME", "").strip()
    if legacy:
        # v0.4 accepted a direct Grok home. Treat it as a managed runtime root
        # so Grok still sees its profile at the native ~/.grok location.
        return Path(legacy).expanduser().resolve()
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "grok-worker"
            / "runtime-home"
        ).resolve()
    data_home = environ.get("XDG_DATA_HOME", "").strip()
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return (base / "grok-worker" / "runtime-home").resolve()


def worker_grok_home(environ: Mapping[str, str] = os.environ) -> Path:
    return worker_runtime_home(environ) / ".grok"


def scoped_worker_grok_home(
    scope: Path | str,
    environ: Mapping[str, str] = os.environ,
) -> Path:
    """Compatibility alias for the stable shared worker profile.

    Per-clone homes changed Grok Build's native prompt prefix and defeated
    provider cache reuse. The scope is intentionally ignored in v0.5.
    """
    Path(scope).expanduser().resolve()
    return worker_grok_home(environ)


def _toml_key(value: str) -> str:
    return value if _BARE_KEY.fullmatch(value) else json.dumps(value, ensure_ascii=False)


def _toml_value(value: object, *, field: str) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GrokProfileError(f"unsupported non-finite TOML value for {field}")
        return repr(value)
    if isinstance(value, list):
        rendered = [_toml_value(item, field=field) for item in value]
        return "[" + ", ".join(rendered) + "]"
    raise GrokProfileError(f"unsupported TOML value for {field}: {type(value).__name__}")


def _table_lines(name: str, values: Mapping[str, object]) -> list[str]:
    lines = [f"[{name}]"]
    for key, value in values.items():
        if value is None:
            continue
        lines.append(f"{_toml_key(key)} = {_toml_value(value, field=f'{name}.{key}')}" )
    return lines


def _credential(model: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    inline = model.get("api_key")
    if isinstance(inline, str) and inline.strip():
        return inline.strip()
    configured = model.get("env_key")
    names: list[str]
    if isinstance(configured, str):
        names = [configured]
    elif isinstance(configured, list) and all(isinstance(item, str) for item in configured):
        names = configured
    else:
        names = []
    for name in names:
        value = environ.get(name, "").strip()
        if value:
            return value
    raise GrokProfileError(
        "active model has no resolvable API key; configure api_key or env_key "
        "in the source Grok profile"
    )


def _derived_config(
    source: Mapping[str, Any],
    *,
    model_id: str,
    reasoning_effort: str,
) -> tuple[str, Mapping[str, Any]]:
    model_root = source.get("model")
    if not isinstance(model_root, dict):
        raise GrokProfileError("source Grok config has no [model] table")
    raw_model = model_root.get(model_id)
    if not isinstance(raw_model, dict):
        raise GrokProfileError(f"source Grok config has no model profile for {model_id!r}")
    model_values = {
        key: value
        for key, value in raw_model.items()
        if key not in {"api_key", "env_key"}
    }
    # A credential-free isolated home cannot refresh Grok's official catalog.
    # Declare the requested capability on the custom profile so Build does not
    # silently discard --reasoning-effort even though the upstream model accepts it.
    model_values["reasoning_effort"] = reasoning_effort
    model_values["supports_reasoning_effort"] = True
    model_values["env_key"] = WORKER_API_KEY_ENV
    sections = [
        _table_lines("cli", {"auto_update": False}),
        _table_lines("compat.claude", {"mcps": False}),
        _table_lines("compat.cursor", {"mcps": False}),
        _table_lines("plugins", {"enabled": [], "disabled": ["*"]}),
        _table_lines("ui", {"fork_secondary_model": model_id}),
        _table_lines(
            "models",
            {
                "default": model_id,
                "session_summary": model_id,
                "default_reasoning_effort": reasoning_effort,
            },
        ),
        _table_lines(f"model.{_toml_key(model_id)}", model_values),
    ]
    return "\n\n".join("\n".join(section) for section in sections) + "\n", raw_model


def _ensure_private_home(home: Path) -> None:
    if home.is_symlink():
        raise GrokProfileError(f"worker Grok home must not be a symlink: {home}")
    if home.exists() and not home.is_dir():
        raise GrokProfileError(f"worker Grok home is not a directory: {home}")
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(home, 0o700)


def _claim_managed_home(home: Path) -> None:
    marker = home / PROFILE_MARKER
    unmanaged_entries = [path for path in home.iterdir() if path.name != PROFILE_LOCK]
    if unmanaged_entries and not marker.is_file():
        raise GrokProfileError(f"refusing unmanaged nonempty worker Grok home: {home}")
    if not marker.exists():
        _atomic_write(marker, "managed by grok-worker\n", 0o600)


@contextmanager
def _profile_lock(home: Path) -> Iterator[None]:
    path = home / PROFILE_LOCK
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_write(path: Path, content: str, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        os.chmod(path, mode)
    finally:
        temp_path.unlink(missing_ok=True)


def _sync_agents(source_home: Path, profile_home: Path) -> None:
    source = source_home / "Agents.md"
    destination = profile_home / "Agents.md"
    if not source.is_file():
        if destination.is_symlink():
            destination.unlink()
        return
    temporary = profile_home / f".Agents.md.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    try:
        os.symlink(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_model_cache(source_home: Path, profile_home: Path) -> None:
    """Copy model capability metadata without copying auth or session state."""
    source = source_home / "models_cache.json"
    destination = profile_home / "models_cache.json"
    if not source.is_file() or source.is_symlink():
        destination.unlink(missing_ok=True)
        return
    try:
        text = source.read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise GrokProfileError(f"cannot read source Grok model cache: {exc}") from exc
    if not isinstance(payload, (dict, list)):
        raise GrokProfileError("source Grok model cache has an invalid payload")
    _atomic_write(destination, text, 0o600)


def isolated_child_environment(
    environ: Mapping[str, str], profile: PreparedGrokProfile
) -> dict[str, str]:
    """Apply the isolated native-home environment without GROK_HOME override mode."""
    child = dict(environ)
    child.pop("GROK_HOME", None)
    child.update(profile.environment)
    return child


def prepare_isolated_profile(
    *,
    model_id: str,
    reasoning_effort: str,
    environ: Mapping[str, str] = os.environ,
) -> PreparedGrokProfile:
    source_home = source_grok_home(environ)
    runtime_root = worker_runtime_home(environ)
    config_path = source_home / "config.toml"
    try:
        with config_path.open("rb") as stream:
            source = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GrokProfileError(f"cannot read source Grok config: {config_path}: {exc}") from exc
    config_text, raw_model = _derived_config(
        source, model_id=model_id, reasoning_effort=reasoning_effort
    )
    # Model ID alone is unsafe when profiles route it to different providers.
    # Keep reuse for identical source/config/effort tuples only.
    profile_identity = f"{source_home}\0{config_text}".encode()
    profile_key = hashlib.sha256(profile_identity).hexdigest()[:16]
    runtime_home = runtime_root / "profiles" / profile_key
    profile_home = runtime_home / ".grok"
    if source_home == profile_home:
        raise GrokProfileError("worker Grok home must differ from the source Grok home")
    key = _credential(raw_model, environ)
    _ensure_private_home(runtime_home)
    _ensure_private_home(profile_home)
    with _profile_lock(profile_home):
        _claim_managed_home(profile_home)
        _atomic_write(profile_home / "config.toml", config_text, 0o600)
        _sync_agents(source_home, profile_home)
        _sync_model_cache(source_home, profile_home)
    child_environment = {
        # Grok Build 0.2.103 loses explicit reasoning effort and provider cache
        # behavior when launched in non-native GROK_HOME override mode. Give it
        # an isolated HOME with a normal ~/.grok instead.
        "HOME": str(runtime_home),
        "GROK_WORKER_RUNTIME_HOME": str(runtime_home),
        "GROK_WORKER_SOURCE_GROK_HOME": str(source_home),
        WORKER_API_KEY_ENV: key,
    }
    return PreparedGrokProfile(profile_home, runtime_home, source_home, child_environment)


def validate_isolated_profile(
    *,
    grok_bin: str,
    profile: PreparedGrokProfile,
    environ: Mapping[str, str],
    cwd: Path,
    allow_extensions: bool = False,
) -> None:
    child_env = isolated_child_environment(environ, profile)
    try:
        completed = subprocess.run(
            [grok_bin, "inspect", "--json"],
            cwd=cwd,
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GrokProfileError(f"isolated profile inspect failed: {exc}") from exc
    if completed.returncode != 0:
        raise GrokProfileError(
            f"isolated profile inspect exited {completed.returncode}; "
            "see worker log for Grok diagnostics"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GrokProfileError("isolated profile inspect did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise GrokProfileError("isolated profile inspect returned an invalid payload")
    config_sources = payload.get("configSources")
    if not isinstance(config_sources, dict):
        raise GrokProfileError("isolated profile inspect omitted config sources")
    layers = config_sources.get("layers")
    if not isinstance(layers, list):
        raise GrokProfileError("isolated profile inspect returned invalid config layers")
    user_paths = [
        layer.get("path")
        for layer in layers
        if isinstance(layer, dict) and layer.get("role") == "user"
    ]
    expected = (profile.home / "config.toml").resolve()
    if not any(isinstance(path, str) and Path(path).resolve() == expected for path in user_paths):
        raise GrokProfileError("isolated profile inspect did not load the managed user config")
    if allow_extensions:
        return
    plugins = payload.get("plugins")
    mcp_servers = payload.get("mcpServers")
    if not isinstance(plugins, list) or not isinstance(mcp_servers, list):
        raise GrokProfileError("isolated profile inspect returned invalid extension lists")
    if plugins or mcp_servers:
        plugin_names = [item.get("name", "unknown") for item in plugins if isinstance(item, dict)]
        mcp_names = [
            item.get("name", "unknown") for item in mcp_servers if isinstance(item, dict)
        ]
        names = ", ".join([*plugin_names, *mcp_names]) or "unknown extension"
        raise GrokProfileError(
            f"isolated profile unexpectedly loaded plugins or MCP servers: {names}"
        )
