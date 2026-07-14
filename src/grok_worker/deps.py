"""Shared uv dependency environments: fingerprint, frozen-only, no local env."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from shutil import which

from grok_worker.locks import fingerprint_lock

SYNC_CONTRACT: tuple[str, ...] = (
    "--frozen",
    "--all-groups",
    "--all-extras",
    "--no-install-project",
)
READY_MARKER_NAME = ".grok-worker-ready"


class DepsError(RuntimeError):
    """Shared dependency preparation failed (hard failure)."""


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def interpreter_identity(executable: str | None = None) -> str:
    """Stable interpreter identity across ephemeral launcher symlink paths.

    ``uv run --no-project --with ...`` exposes a fresh
    ``.../builds-v0/.tmpXXXX/bin/python`` as ``sys.executable`` each launch, while
    ``os.path.realpath`` of that path points at the same managed interpreter.
    Hash the resolved path so shared-env fingerprints stay stable across launches.
    """
    raw = executable if executable is not None else sys.executable
    try:
        return os.path.realpath(raw)
    except OSError:
        return raw


def compute_fingerprint(source: Path) -> str:
    """Fingerprint: dep inputs + Python identity + platform + exact sync options."""
    h = hashlib.sha256()
    h.update(platform.system().encode())
    h.update(platform.machine().encode())
    h.update(sys.version.encode())
    h.update(interpreter_identity().encode())
    for name in ("uv.lock", "pyproject.toml", "requirements.txt", "requirements.lock"):
        p = source / name
        if p.is_file() and not p.is_symlink():
            h.update(name.encode())
            h.update(_hash_file(p).encode())
    h.update(" ".join(SYNC_CONTRACT).encode())
    return h.hexdigest()[:24]


def shared_paths(shared_root: Path, fingerprint: str) -> tuple[Path, Path, Path]:
    root = shared_root.resolve()
    return root / "uv", root / "venvs" / fingerprint, root / "locks"


def build_uv_sync_cmd(source: Path, *, has_lock: bool) -> list[str]:
    if not has_lock:
        raise DepsError("uv.lock required for frozen sync")
    return [
        "uv",
        "sync",
        *SYNC_CONTRACT,
        "--directory",
        str(source.resolve()),
    ]


def _interpreter_present(venv: Path) -> bool:
    for name in ("python", "python3"):
        p = venv / "bin" / name
        if p.is_file() and not p.is_symlink():
            return True
        # allow symlink to real interpreter inside shared env
        if p.is_file() or (p.is_symlink() and p.exists()):
            return True
    return False


def _marker_payload(fingerprint: str) -> dict[str, object]:
    return {
        "fingerprint": fingerprint,
        "sync_contract": list(SYNC_CONTRACT),
    }


def _ready_marker_valid(venv: Path, fingerprint: str) -> bool:
    marker = venv / READY_MARKER_NAME
    if not marker.is_file() or marker.is_symlink():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("fingerprint") != fingerprint:
        return False
    if data.get("sync_contract") != list(SYNC_CONTRACT):
        return False
    return _interpreter_present(venv)


def _write_ready_marker(venv: Path, fingerprint: str) -> None:
    marker = venv / READY_MARKER_NAME
    tmp = venv / f".{READY_MARKER_NAME}.tmp.{os.getpid()}"
    payload = json.dumps(_marker_payload(fingerprint), sort_keys=True, indent=2) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, marker)


def prepare_shared_env(
    source: Path,
    shared_root: Path,
    *,
    allow_unpinned: bool = False,
) -> dict[str, str]:
    """Prepare or reuse a fingerprint-keyed shared env. Never clone-local.

    Under the fingerprint lock: if interpreter + matching ready marker exist,
    skip uv sync. Otherwise run the single frozen command, validate, write marker.
    """
    source = source.resolve()
    shared_root = shared_root.resolve()
    has_inputs = any(
        (source / n).is_file()
        for n in ("uv.lock", "pyproject.toml", "requirements.txt", "requirements.lock")
    )
    if not has_inputs:
        return {}
    has_lock = (source / "uv.lock").is_file()
    if not has_lock:
        if not allow_unpinned:
            raise DepsError(
                "dependency inputs present but no uv.lock; pass explicit "
                "allow_unpinned/opt-in or add a lockfile (refusing unpinned sync)"
            )
        raise DepsError("unpinned sync is not supported in this lifecycle build")
    if which("uv") is None:
        raise DepsError("uv not found on PATH")

    fp = compute_fingerprint(source)
    uv_cache, venv, _ = shared_paths(shared_root, fp)
    with fingerprint_lock(shared_root, fp):
        if _ready_marker_valid(venv, fp):
            pass  # reuse
        else:
            uv_cache.mkdir(parents=True, exist_ok=True)
            venv.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["UV_CACHE_DIR"] = str(uv_cache)
            env["UV_PROJECT_ENVIRONMENT"] = str(venv)
            cmd = build_uv_sync_cmd(source, has_lock=True)
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
            except subprocess.CalledProcessError as exc:
                raise DepsError(exc.stderr or str(exc)) from exc
            if not _interpreter_present(venv):
                raise DepsError(f"uv sync completed but interpreter missing in shared env: {venv}")
            _write_ready_marker(venv, fp)

    clone_src = source.resolve()
    return {
        "UV_CACHE_DIR": str(uv_cache),
        "UV_PROJECT_ENVIRONMENT": str(venv),
        "PYTHONPATH": f"{clone_src}{os.pathsep}{clone_src / 'src'}",
        "GROK_WORKER_DEPS_FINGERPRINT": fp,
    }


def worker_env_exports(env_vars: dict[str, str]) -> str:
    """Prompt contract requiring uv run --no-sync."""
    lines = [
        "# Shared dependency contract (MANDATORY):",
        "#   Always use: uv run --no-sync <command>",
        "#   Never: uv sync / pip install inside the clone",
        "#   Never create clone-local .venv",
    ]
    for key in (
        "UV_CACHE_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "PYTHONPATH",
        "GROK_WORKER_DEPS_FINGERPRINT",
    ):
        if key in env_vars:
            lines.append(f"export {key}={env_vars[key]!r}")
    return "\n".join(lines) + "\n"


def detect_clone_local_env(clone: Path) -> list[str]:
    """Return names of clone-local Python environments (failure signal)."""
    found: list[str] = []
    if not clone.is_dir():
        return found
    for child in clone.iterdir():
        if child.is_symlink():
            continue
        name = child.name
        if name == ".venv" or name.startswith(".venv-") or name == "venv":
            if child.is_dir():
                found.append(name)
    return found
