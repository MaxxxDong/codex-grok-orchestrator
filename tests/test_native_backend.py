"""Native Grok headless backend lifecycle and isolation coverage."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from grok_worker.runner import RunConfig, run_worker


def _source_home(tmp_path: Path) -> Path:
    home = tmp_path / "source-grok"
    home.mkdir()
    (home / "Agents.md").write_text("worker rules\n", encoding="utf-8")
    (home / "models_cache.json").write_text("{}\n", encoding="utf-8")
    (home / "config.toml").write_text(
        """
[models]
default = "grok-4.5"
default_reasoning_effort = "high"

[model."grok-4.5"]
model = "grok-4.5"
base_url = "https://example.invalid/v1"
api_backend = "responses"
api_key = "test-secret-value"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return home


def _fake_grok(bin_dir: Path, *, downgrade_reasoning: bool = False) -> Path:
    if os.name == "nt":
        grok = bin_dir / "grok.cmd"
        grok.parent.mkdir(parents=True, exist_ok=True)
        warning = (
            "echo model does not support reasoning effort; ignoring 1>&2\r\n"
            if downgrade_reasoning
            else ""
        )
        grok.write_text(
            "@echo off\r\n"
            "setlocal EnableDelayedExpansion\r\n"
            "if \"%~1\"==\"inspect\" (\r\n"
            "  set \"config=%USERPROFILE%\\.grok\\config.toml\"\r\n"
            "  set \"config=!config:\\=\\\\!\"\r\n"
            "  echo {\"configSources\":{\"layers\":[{\"role\":\"user\","
            "\"path\":\"!config!\"}]},\"plugins\":[],\"mcpServers\":[]}\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            "set \"cwd=\"\r\n"
            "set \"args=\"\r\n"
            ":args\r\n"
            "if \"%~1\"==\"\" goto run\r\n"
            "set \"args=!args! %~1\"\r\n"
            "if \"%~1\"==\"--cwd\" (\r\n"
            "  set \"cwd=%~2\"\r\n"
            ")\r\n"
            "shift\r\n"
            "goto args\r\n"
            ":run\r\n"
            "if not defined cwd exit /b 90\r\n"
            "if exist \"!cwd!\\.mcp.json\" exit /b 91\r\n"
            "mkdir \"!cwd!\\.grok-output\\verification\" 2>nul\r\n"
            "> \"!cwd!\\.grok-output\\verification\\verify.log\" echo verification passed\r\n"
            "> \"!cwd!\\.grok-output\\result.json\" echo {\"schema_version\":1,"
            "\"task_completed\":true,\"status\":\"completed\","
            "\"summary\":\"native ok\",\"findings\":[],\"verification\":[{"
            "\"command\":\"fake verify\",\"exit_code\":0,"
            "\"log_path\":\".grok-output/verification/verify.log\"}]}\r\n"
            "> \"!cwd!\\.grok-output\\native-env.txt\" (\r\n"
            "  echo %HOME%\r\n"
            "  if defined GROK_HOME (echo %GROK_HOME%) else (echo unset)\r\n"
            "  echo !args!\r\n"
            "  echo %UV_CACHE_DIR%\r\n"
            "  echo %GROK_SHARED_VENV_ROOT%\r\n"
            ")\r\n"
            "echo {\"text\":\"native ok\",\"thought\":\"checked\","
            "\"usage\":{\"input_tokens\":4096,\"cache_read_input_tokens\":2048,"
            "\"output_tokens\":128,\"reasoning_tokens\":77}}\r\n"
            + warning,
            encoding="utf-8",
        )
        return grok
    grok = bin_dir / "grok"
    grok.parent.mkdir(parents=True, exist_ok=True)
    warning = (
        "printf '%s\\n' 'model does not support reasoning effort; ignoring' >&2\n"
        if downgrade_reasoning
        else ""
    )
    grok.write_text(
        r"""#!/bin/sh
set -eu
if [ "${1:-}" = "inspect" ]; then
  printf '%s\n' \
    "{\"configSources\":{\"layers\":[{\"role\":\"user\",\"path\":\"$HOME/.grok/config.toml\"}]},"\
"\"plugins\":[],\"mcpServers\":[]}"
  exit 0
fi
cwd=""
args=""
while [ "$#" -gt 0 ]; do
  args="$args $1"
  if [ "$1" = "--cwd" ]; then shift; cwd="$1"; fi
  shift
done
test -n "$cwd"
test ! -e "$cwd/.mcp.json"
mkdir -p "$cwd/.grok-output/verification"
printf 'verification passed\n' > "$cwd/.grok-output/verification/verify.log"
cat > "$cwd/.grok-output/result.json" <<'JSON'
{
  "schema_version": 1,
  "task_completed": true,
  "status": "completed",
  "summary": "native ok",
  "findings": [],
  "verification": [{
    "command": "fake verify",
    "exit_code": 0,
    "log_path": ".grok-output/verification/verify.log"
  }]
}
JSON
printf '%s\n%s\n%s\n%s\n%s\n' \
  "$HOME" "${GROK_HOME-unset}" "$args" "$UV_CACHE_DIR" "$GROK_SHARED_VENV_ROOT" \
  > "$cwd/.grok-output/native-env.txt"
printf '%s%s\n' \
  '{"text":"native ok","thought":"checked","usage":{"input_tokens":4096,' \
  '"cache_read_input_tokens":2048,"output_tokens":128,"reasoning_tokens":77}}'
"""
        + warning,
        encoding="utf-8",
    )
    grok.chmod(0o755)
    return grok


def test_native_backend_uses_high_stable_home_and_masks_project_mcp(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_grok(tmp_roots["root"] / "bin")
    source_home = _source_home(tmp_roots["root"])
    (git_source / ".mcp.json").write_text(
        '{"mcpServers":{"unsafe":{"command":"false"}}}\n', encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(git_source), "add", ".mcp.json"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_source), "commit", "-m", "mcp"],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", str(fake))
    monkeypatch.setenv("GROK_WORKER_SOURCE_GROK_HOME", str(source_home))

    outcome = run_worker(
        RunConfig(
            source=git_source,
            prompt="implement and verify",
            disposable_root=tmp_roots["disposable"],
            artifact_root=tmp_roots["artifacts"],
            shared_cache_root=tmp_roots["shared"],
            backend="native",
            acpx_bin="definitely-not-installed",
            agent_bin="definitely-not-installed",
            prepare_deps=False,
            task_id="native-ok",
            keep_reason="inspect native test clone",
            skip_post_gc=True,
        )
    )

    assert outcome.exit_code == 0
    assert outcome.state == "keep"
    clone = Path(outcome.clone_path or "")
    assert (clone / ".mcp.json").is_file()
    home, grok_home, args, uv_cache, shared_venvs = (
        (clone / ".grok-output/native-env.txt").read_text(encoding="utf-8").splitlines()
    )
    runtime_home = Path(home)
    assert runtime_home.parent == tmp_roots["shared"] / "grok-runtime-home/profiles"
    assert len(runtime_home.name) == 16
    assert grok_home == "unset"
    assert "--reasoning-effort high" in args
    assert "--prompt-file" in args
    assert Path(uv_cache) == clone / ".grok-output/.runtime-cache/uv"
    assert Path(shared_venvs) == tmp_roots["shared"] / "venvs"

    metrics = [
        json.loads(line)
        for line in (tmp_roots["shared"] / "metrics/worker-runs.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert metrics[-1]["backend"] == "native"
    assert metrics[-1]["cached_tokens"] == 2048
    assert metrics[-1]["reasoning_tokens"] == 77


def test_native_backend_rejects_reasoning_downgrade(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_grok(tmp_roots["root"] / "bin", downgrade_reasoning=True)
    source_home = _source_home(tmp_roots["root"])
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", str(fake))
    monkeypatch.setenv("GROK_WORKER_SOURCE_GROK_HOME", str(source_home))

    outcome = run_worker(
        RunConfig(
            source=git_source,
            prompt="implement and verify",
            disposable_root=tmp_roots["disposable"],
            artifact_root=tmp_roots["artifacts"],
            shared_cache_root=tmp_roots["shared"],
            backend="native",
            prepare_deps=False,
            task_id="native-reasoning-downgrade",
            skip_post_gc=True,
        )
    )

    assert outcome.exit_code == 78
    assert outcome.state == "failed"
    assert "ignored requested reasoning effort" in outcome.message


def test_retained_task_id_collision_allocates_fresh_clone(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
) -> None:
    base = dict(
        source=git_source,
        prompt="x",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        task_id="same-task",
        keep_reason="retain collision evidence",
        skip_post_gc=True,
    )
    first = run_worker(RunConfig(**base))
    second = run_worker(RunConfig(**base))
    assert first.task_id == "same-task"
    assert second.task_id.startswith("same-task-")
    assert first.clone_path != second.clone_path
