"""Isolated Grok profile derivation and fail-closed extension checks."""

from __future__ import annotations

import os
import stat
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from grok_worker.grok_profile import (
    PROFILE_MARKER,
    WORKER_API_KEY_ENV,
    GrokProfileError,
    prepare_isolated_profile,
    scoped_worker_grok_home,
    validate_isolated_profile,
)


def _source_home(tmp_path: Path, credential: str = 'api_key = "secret-value"') -> Path:
    source = tmp_path / "source-home"
    source.mkdir()
    (source / "Agents.md").write_text("worker rules\n", encoding="utf-8")
    (source / "config.toml").write_text(
        f"""
[cli]
auto_update = true

[marketplace]
official_marketplace_auto_installed = true

[plugins]
enabled = ["browser-plugin"]

[models]
default = "grok-4.5"
default_reasoning_effort = "medium"

[model."grok-4.5"]
model = "grok-4.5"
base_url = "https://example.invalid/v1"
api_backend = "responses"
context_window = 300000
{credential}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return source


def test_profile_strips_extensions_and_keeps_secret_out_of_config(tmp_path: Path) -> None:
    source = _source_home(tmp_path)
    profile_home = tmp_path / "worker-home"
    profile = prepare_isolated_profile(
        model_id="grok-4.5",
        reasoning_effort="high",
        environ={
            "GROK_WORKER_SOURCE_GROK_HOME": str(source),
            "GROK_WORKER_GROK_HOME": str(profile_home),
        },
    )

    data = tomllib.loads((profile_home / "config.toml").read_text(encoding="utf-8"))
    assert data["models"] == {
        "default": "grok-4.5",
        "default_reasoning_effort": "high",
    }
    assert data["ui"]["fork_secondary_model"] == "grok-4.5"
    assert data["model"]["grok-4.5"]["env_key"] == WORKER_API_KEY_ENV
    assert "api_key" not in data["model"]["grok-4.5"]
    assert data["claude_compat"] == {"imported": True}
    assert "plugins" not in data
    assert "marketplace" not in data
    assert "secret-value" not in (profile_home / "config.toml").read_text(encoding="utf-8")
    assert profile.environment[WORKER_API_KEY_ENV] == "secret-value"
    assert (profile_home / "Agents.md").is_symlink()
    assert (profile_home / "Agents.md").resolve() == (source / "Agents.md").resolve()
    assert stat.S_IMODE(profile_home.stat().st_mode) == 0o700
    assert stat.S_IMODE((profile_home / "config.toml").stat().st_mode) == 0o600
    assert (profile_home / PROFILE_MARKER).is_file()


def test_profile_resolves_source_env_key(tmp_path: Path) -> None:
    source = _source_home(tmp_path, 'env_key = ["MISSING_KEY", "LIVE_KEY"]')
    profile = prepare_isolated_profile(
        model_id="grok-4.5",
        reasoning_effort="high",
        environ={
            "GROK_WORKER_SOURCE_GROK_HOME": str(source),
            "GROK_WORKER_GROK_HOME": str(tmp_path / "worker-home"),
            "LIVE_KEY": "resolved-value",
        },
    )
    assert profile.environment[WORKER_API_KEY_ENV] == "resolved-value"


def test_profile_refuses_missing_credentials(tmp_path: Path) -> None:
    source = _source_home(tmp_path, 'env_key = "MISSING_KEY"')
    with pytest.raises(GrokProfileError, match="no resolvable API key"):
        prepare_isolated_profile(
            model_id="grok-4.5",
            reasoning_effort="high",
            environ={
                "GROK_WORKER_SOURCE_GROK_HOME": str(source),
                "GROK_WORKER_GROK_HOME": str(tmp_path / "worker-home"),
            },
        )


def test_profile_refuses_unmanaged_nonempty_home(tmp_path: Path) -> None:
    source = _source_home(tmp_path)
    profile_home = tmp_path / "worker-home"
    profile_home.mkdir()
    (profile_home / "user-file").write_text("do not overwrite\n", encoding="utf-8")
    with pytest.raises(GrokProfileError, match="unmanaged nonempty"):
        prepare_isolated_profile(
            model_id="grok-4.5",
            reasoning_effort="high",
            environ={
                "GROK_WORKER_SOURCE_GROK_HOME": str(source),
                "GROK_WORKER_GROK_HOME": str(profile_home),
            },
        )


def test_profile_first_refresh_is_concurrency_safe(tmp_path: Path) -> None:
    source = _source_home(tmp_path)
    profile_home = tmp_path / "worker-home"

    def prepare() -> str:
        profile = prepare_isolated_profile(
            model_id="grok-4.5",
            reasoning_effort="high",
            environ={
                "GROK_WORKER_SOURCE_GROK_HOME": str(source),
                "GROK_WORKER_GROK_HOME": str(profile_home),
            },
        )
        return profile.environment[WORKER_API_KEY_ENV]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: prepare(), range(16))) == ["secret-value"] * 16
    assert tomllib.loads((profile_home / "config.toml").read_text(encoding="utf-8"))
    assert (profile_home / PROFILE_MARKER).is_file()


def test_scoped_worker_homes_are_stable_and_isolated(tmp_path: Path) -> None:
    environ = {"GROK_WORKER_GROK_HOME": str(tmp_path / "managed-root")}
    first = scoped_worker_grok_home(tmp_path / "clone-a", environ)
    repeated = scoped_worker_grok_home(tmp_path / "clone-a", environ)
    second = scoped_worker_grok_home(tmp_path / "clone-b", environ)

    assert first == repeated
    assert first != second
    assert first.parent == tmp_path / "managed-root" / "workers"


def test_concurrent_models_use_distinct_managed_homes(tmp_path: Path) -> None:
    source = _source_home(tmp_path)
    with (source / "config.toml").open("a", encoding="utf-8") as stream:
        stream.write(
            '\n[model."grok-fast"]\n'
            'model = "grok-fast"\n'
            'base_url = "https://example.invalid/v1"\n'
            'api_backend = "responses"\n'
            'api_key = "other-secret"\n'
        )
    base_environ = {
        "GROK_WORKER_SOURCE_GROK_HOME": str(source),
        "GROK_WORKER_GROK_HOME": str(tmp_path / "managed-root"),
    }

    def prepare(model: str, clone: str) -> Path:
        environ = dict(base_environ)
        environ["GROK_WORKER_GROK_HOME"] = str(
            scoped_worker_grok_home(tmp_path / clone, base_environ)
        )
        return prepare_isolated_profile(
            model_id=model,
            reasoning_effort="high",
            environ=environ,
        ).home

    with ThreadPoolExecutor(max_workers=2) as pool:
        grok_home, fast_home = list(
            pool.map(lambda args: prepare(*args), [("grok-4.5", "a"), ("grok-fast", "b")])
        )

    assert grok_home != fast_home
    assert tomllib.loads((grok_home / "config.toml").read_text(encoding="utf-8"))[
        "models"
    ]["default"] == "grok-4.5"
    assert tomllib.loads((fast_home / "config.toml").read_text(encoding="utf-8"))[
        "models"
    ]["default"] == "grok-fast"


def _fake_grok(tmp_path: Path, payload: str) -> Path:
    script = tmp_path / "grok"
    script.write_text(
        "#!/bin/sh\nprintf '%s\\n' '" + payload.replace("'", "'\\''") + "'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_inspect_accepts_managed_config_without_extensions(tmp_path: Path) -> None:
    source = _source_home(tmp_path)
    profile = prepare_isolated_profile(
        model_id="grok-4.5",
        reasoning_effort="high",
        environ={
            "GROK_WORKER_SOURCE_GROK_HOME": str(source),
            "GROK_WORKER_GROK_HOME": str(tmp_path / "worker-home"),
        },
    )
    payload = (
        '{"configSources":{"layers":[{"role":"user","path":"'
        + str(profile.home / "config.toml")
        + '"}]},"plugins":[],"mcpServers":[]}'
    )
    validate_isolated_profile(
        grok_bin=str(_fake_grok(tmp_path, payload)),
        profile=profile,
        environ=os.environ,
        cwd=tmp_path,
    )


def test_inspect_rejects_plugins_and_mcp(tmp_path: Path) -> None:
    source = _source_home(tmp_path)
    profile = prepare_isolated_profile(
        model_id="grok-4.5",
        reasoning_effort="high",
        environ={
            "GROK_WORKER_SOURCE_GROK_HOME": str(source),
            "GROK_WORKER_GROK_HOME": str(tmp_path / "worker-home"),
        },
    )
    payload = (
        '{"configSources":{"layers":[{"role":"user","path":"'
        + str(profile.home / "config.toml")
        + '"}]},"plugins":[{"name":"bad-plugin"}],'
        '"mcpServers":[{"name":"bad-mcp"}]}'
    )
    with pytest.raises(GrokProfileError, match="bad-plugin, bad-mcp"):
        validate_isolated_profile(
            grok_bin=str(_fake_grok(tmp_path, payload)),
            profile=profile,
            environ=os.environ,
            cwd=tmp_path,
        )


def test_agent_entry_uses_isolated_profile_for_grok_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_home(tmp_path)
    profile_home = tmp_path / "worker-home"
    record = tmp_path / "child-env.txt"
    script = tmp_path / "grok-agent"
    script.write_text(
        """#!/bin/sh
if [ "${1:-}" = "inspect" ]; then
  printf '%s\\n' \
    "{\\"configSources\\":{\\"layers\\":[{\\"role\\":\\"user\\",\\"path\\":\\"$GROK_HOME/config.toml\\"}]},\\"plugins\\":[],\\"mcpServers\\":[]}"
  exit 0
fi
printf '%s\\n%s\\n' "$GROK_HOME" "$GROK_WORKER_API_KEY" > "$GROK_TEST_RECORD"
exit 0
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROK_WORKER_LIFECYCLE", "1")
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", str(script))
    monkeypatch.setenv("GROK_WORKER_SOURCE_GROK_HOME", str(source))
    monkeypatch.setenv("GROK_WORKER_GROK_HOME", str(profile_home))
    monkeypatch.setenv("GROK_TEST_RECORD", str(record))

    from grok_worker.agent_entry import main

    assert main() == 0
    assert record.read_text(encoding="utf-8").splitlines() == [
        str(profile_home),
        "secret-value",
    ]
