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
    runtime_home = tmp_path / "worker-home"
    profile = prepare_isolated_profile(
        model_id="grok-4.5",
        reasoning_effort="high",
        environ={
            "GROK_WORKER_SOURCE_GROK_HOME": str(source),
            "GROK_WORKER_RUNTIME_HOME": str(runtime_home),
        },
    )

    profile_home = profile.home
    data = tomllib.loads((profile_home / "config.toml").read_text(encoding="utf-8"))
    assert data["models"] == {
        "default": "grok-4.5",
        "session_summary": "grok-4.5",
        "default_reasoning_effort": "high",
    }
    assert data["ui"]["fork_secondary_model"] == "grok-4.5"
    assert data["model"]["grok-4.5"]["env_key"] == WORKER_API_KEY_ENV
    assert data["model"]["grok-4.5"]["reasoning_effort"] == "high"
    assert data["model"]["grok-4.5"]["supports_reasoning_effort"] is True
    assert "api_key" not in data["model"]["grok-4.5"]
    assert data["compat"]["claude"] == {"mcps": False}
    assert data["compat"]["cursor"] == {"mcps": False}
    assert data["plugins"] == {"enabled": [], "disabled": ["*"]}
    assert "marketplace" not in data
    assert "secret-value" not in (profile_home / "config.toml").read_text(encoding="utf-8")
    assert profile.environment[WORKER_API_KEY_ENV] == "secret-value"
    agents = profile_home / "Agents.md"
    if os.name == "nt":
        assert agents.read_text(encoding="utf-8") == "worker rules\n"
        assert profile.environment["USERPROFILE"] == str(profile.runtime_home)
    else:
        assert agents.is_symlink()
        assert agents.resolve() == (source / "Agents.md").resolve()
    if os.name != "nt":
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
            "GROK_WORKER_RUNTIME_HOME": str(tmp_path / "worker-home"),
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
                "GROK_WORKER_RUNTIME_HOME": str(tmp_path / "worker-home"),
            },
        )


def test_profile_refuses_unmanaged_nonempty_home(tmp_path: Path) -> None:
    source = _source_home(tmp_path)
    runtime_home = tmp_path / "worker-home"
    probe = prepare_isolated_profile(
        model_id="grok-4.5",
        reasoning_effort="high",
        environ={
            "GROK_WORKER_SOURCE_GROK_HOME": str(source),
            "GROK_WORKER_RUNTIME_HOME": str(tmp_path / "probe-home"),
        },
    )
    profile_home = runtime_home / "profiles" / probe.runtime_home.name / ".grok"
    profile_home.mkdir(parents=True)
    (profile_home / "user-file").write_text("do not overwrite\n", encoding="utf-8")
    with pytest.raises(GrokProfileError, match="unmanaged nonempty"):
        prepare_isolated_profile(
            model_id="grok-4.5",
            reasoning_effort="high",
            environ={
                "GROK_WORKER_SOURCE_GROK_HOME": str(source),
                "GROK_WORKER_RUNTIME_HOME": str(runtime_home),
            },
        )


def test_profile_first_refresh_is_concurrency_safe(tmp_path: Path) -> None:
    source = _source_home(tmp_path)
    runtime_home = tmp_path / "worker-home"

    def prepare() -> str:
        profile = prepare_isolated_profile(
            model_id="grok-4.5",
            reasoning_effort="high",
            environ={
                "GROK_WORKER_SOURCE_GROK_HOME": str(source),
                "GROK_WORKER_RUNTIME_HOME": str(runtime_home),
            },
        )
        return profile.environment[WORKER_API_KEY_ENV]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: prepare(), range(16))) == ["secret-value"] * 16
    profile_homes = list((runtime_home / "profiles").glob("*/.grok"))
    assert len(profile_homes) == 1
    profile_home = profile_homes[0]
    assert tomllib.loads((profile_home / "config.toml").read_text(encoding="utf-8"))
    assert (profile_home / PROFILE_MARKER).is_file()


def test_scoped_worker_homes_are_stable_and_shared(tmp_path: Path) -> None:
    environ = {"GROK_WORKER_RUNTIME_HOME": str(tmp_path / "managed-root")}
    first = scoped_worker_grok_home(tmp_path / "clone-a", environ)
    repeated = scoped_worker_grok_home(tmp_path / "clone-a", environ)
    second = scoped_worker_grok_home(tmp_path / "clone-b", environ)

    assert first == repeated
    assert first == second
    assert first == tmp_path / "managed-root" / ".grok"


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
        "GROK_WORKER_RUNTIME_HOME": str(tmp_path / "managed-root"),
    }

    def prepare(model: str, clone: str) -> Path:
        environ = dict(base_environ)
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


def test_same_model_different_provider_profiles_never_share_home(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_source = _source_home(first_root, 'api_key = "key-first"')
    second_source = _source_home(second_root, 'api_key = "key-second"')
    second_config = second_source / "config.toml"
    second_config.write_text(
        second_config.read_text(encoding="utf-8").replace(
            "https://example.invalid/v1", "https://other.invalid/v1"
        ),
        encoding="utf-8",
    )
    runtime = tmp_path / "shared-runtime"

    first = prepare_isolated_profile(
        model_id="grok-4.5",
        reasoning_effort="high",
        environ={
            "GROK_WORKER_SOURCE_GROK_HOME": str(first_source),
            "GROK_WORKER_RUNTIME_HOME": str(runtime),
        },
    )
    second = prepare_isolated_profile(
        model_id="grok-4.5",
        reasoning_effort="medium",
        environ={
            "GROK_WORKER_SOURCE_GROK_HOME": str(second_source),
            "GROK_WORKER_RUNTIME_HOME": str(runtime),
        },
    )

    assert first.home != second.home
    first_data = tomllib.loads((first.home / "config.toml").read_text(encoding="utf-8"))
    second_data = tomllib.loads((second.home / "config.toml").read_text(encoding="utf-8"))
    assert first_data["model"]["grok-4.5"]["base_url"] == "https://example.invalid/v1"
    assert first_data["models"]["default_reasoning_effort"] == "high"
    assert second_data["model"]["grok-4.5"]["base_url"] == "https://other.invalid/v1"
    assert second_data["models"]["default_reasoning_effort"] == "medium"
    assert first.environment[WORKER_API_KEY_ENV] == "key-first"
    assert second.environment[WORKER_API_KEY_ENV] == "key-second"


def _fake_grok(tmp_path: Path, payload: str) -> Path:
    if os.name == "nt":
        script = tmp_path / "grok.cmd"
        escaped_payload = payload.replace("\\", "\\\\")
        script.write_text(f"@echo off\r\necho {escaped_payload}\r\n", encoding="utf-8")
    else:
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
            "GROK_WORKER_RUNTIME_HOME": str(tmp_path / "worker-home"),
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
            "GROK_WORKER_RUNTIME_HOME": str(tmp_path / "worker-home"),
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
    runtime_home = tmp_path / "worker-home"
    record = tmp_path / "child-env.txt"
    if os.name == "nt":
        prepared = prepare_isolated_profile(
            model_id="grok-4.5",
            reasoning_effort="high",
            environ={
                "GROK_WORKER_SOURCE_GROK_HOME": str(source),
                "GROK_WORKER_RUNTIME_HOME": str(runtime_home),
            },
        )
        config_json = str(prepared.home / "config.toml").replace("\\", "\\\\")
        script = tmp_path / "grok-agent.cmd"
        script.write_text(
            "@echo off\r\n"
            "if \"%~1\"==\"inspect\" (\r\n"
            "  echo {\"configSources\":{\"layers\":[{\"role\":\"user\","
            f"\"path\":\"{config_json}\"}}]}},\"plugins\":[],\"mcpServers\":[]}}\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            "> \"%GROK_TEST_RECORD%\" (\r\n"
            "  echo %HOME%\r\n"
            "  if defined GROK_HOME (echo %GROK_HOME%) else (echo unset)\r\n"
            "  echo %GROK_WORKER_API_KEY%\r\n"
            ")\r\n",
            encoding="utf-8",
        )
    else:
        script = tmp_path / "grok-agent"
        script.write_text(
            """#!/bin/sh
if [ "${1:-}" = "inspect" ]; then
  printf '%s\\n' \
    "{\\"configSources\\":{\\"layers\\":[{\\"role\\":\\"user\\",\\"path\\":\\"$HOME/.grok/config.toml\\"}]},\\"plugins\\":[],\\"mcpServers\\":[]}"
  exit 0
fi
printf '%s\\n%s\\n%s\\n' "$HOME" "${GROK_HOME-unset}" "$GROK_WORKER_API_KEY" > "$GROK_TEST_RECORD"
exit 0
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROK_WORKER_LIFECYCLE", "1")
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", str(script))
    monkeypatch.setenv("GROK_WORKER_SOURCE_GROK_HOME", str(source))
    monkeypatch.setenv("GROK_WORKER_RUNTIME_HOME", str(runtime_home))
    monkeypatch.setenv("GROK_TEST_RECORD", str(record))

    from grok_worker.agent_entry import main

    assert main() == 0
    child_home, grok_home, key = record.read_text(encoding="utf-8").splitlines()
    assert Path(child_home).parent == runtime_home / "profiles"
    assert len(Path(child_home).name) == 16
    assert grok_home == "unset"
    assert key == "secret-value"
