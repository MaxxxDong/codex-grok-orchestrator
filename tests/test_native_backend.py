"""Native Grok headless backend lifecycle and isolation coverage."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from grok_worker.gc import gc_disposable_root
from grok_worker.grok_state import clone_session_root
from grok_worker.models import WorkerMeta, dt_to_iso, utc_now
from grok_worker.paths import meta_path
from grok_worker.runner import RunConfig, run_worker


def _source_home(tmp_path: Path) -> Path:
    home = tmp_path / "home" / ".grok"
    home.mkdir(parents=True)
    (home / "Agents.md").write_text("worker rules\n", encoding="utf-8")
    (home / "models_cache.json").write_text("{}\n", encoding="utf-8")
    test_key = "test-" + "secret-value"
    (home / "config.toml").write_text(
        f"""
[models]
default = "grok-4.5"
default_reasoning_effort = "high"

[model."grok-4.5"]
model = "grok-4.5"
base_url = "https://example.invalid/v1"
api_backend = "responses"
api_key = "{test_key}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return home


def _fake_grok(
    bin_dir: Path, *, downgrade_reasoning: bool = False, inspect_exit: int = 0
) -> Path:
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
            f"  exit /b {inspect_exit}\r\n"
            ")\r\n"
            "set \"cwd=\"\r\n"
            "set \"args=\"\r\n"
            ":args\r\n"
            "if \"%~1\"==\"\" goto run\r\n"
            "set \"args=!args! %~1\"\r\n"
            "if \"%~1\"==\"--cwd\" set \"cwd=%~2\"\r\n"
            "shift\r\n"
            "goto args\r\n"
            ":run\r\n"
            "if not defined cwd exit /b 90\r\n"
            "set \"mcp_state=absent\"\r\n"
            "if exist \"!cwd!\\.mcp.json\" set \"mcp_state=present\"\r\n"
            "mkdir \"!cwd!\\.grok-output\\verification\" 2>nul\r\n"
            "> \"!cwd!\\.grok-output\\verification\\verify.log\" echo verification passed\r\n"
            "> \"!cwd!\\.grok-output\\native-env.txt\" (\r\n"
            "  echo %HOME%\r\n"
            "  if defined GROK_HOME (echo %GROK_HOME%) else (echo unset)\r\n"
            "  echo !args!\r\n"
            "  echo %UV_CACHE_DIR%\r\n"
            "  echo %GROK_SHARED_VENV_ROOT%\r\n"
            "  echo !mcp_state!\r\n"
            ")\r\n"
            "echo {\"schema_version\":1,\"task_completed\":true,"
            "\"status\":\"completed\",\"summary\":\"native ok\","
            "\"findings\":[],\"verification\":[{\"command\":\"fake verify\","
            "\"exit_code\":0,\"log_path\":\".grok-output/verification/verify.log\"}],"
            "\"usage\":{\"input_tokens\":4096,\"cache_read_input_tokens\":2048,"
            "\"output_tokens\":128,\"reasoning_tokens\":77,\"num_turns\":3}}\r\n"
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
mcp_state=absent
if [ -e "$cwd/.mcp.json" ]; then mcp_state=present; fi
mkdir -p "$cwd/.grok-output/verification"
printf 'verification passed\n' > "$cwd/.grok-output/verification/verify.log"
# 0.7: runner owns result.json via --json-schema capture; model emits WorkerResult JSON.
printf '%s\n%s\n%s\n%s\n%s\n%s\n' \
  "$HOME" "${GROK_HOME-unset}" "$args" "$UV_CACHE_DIR" "$GROK_SHARED_VENV_ROOT" "$mcp_state" \
  > "$cwd/.grok-output/native-env.txt"
printf '%s\n' \
  '{"schema_version":1,"task_completed":true,"status":"completed",'\
'"summary":"native ok","findings":[],"verification":[{"command":"fake verify",'\
'"exit_code":0,"log_path":".grok-output/verification/verify.log"}],'\
'"usage":{"input_tokens":4096,"cache_read_input_tokens":2048,'\
'"output_tokens":128,"reasoning_tokens":77,"num_turns":3}}'
"""
        + warning,
        encoding="utf-8",
    )
    grok.chmod(0o755)
    return grok


def test_native_backend_uses_source_home_high_and_project_mcp(
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
    monkeypatch.setenv("HOME", str(source_home.parent))

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
    home, grok_home, args, uv_cache, shared_venvs, mcp_state = (
        (clone / ".grok-output/native-env.txt").read_text(encoding="utf-8").splitlines()
    )
    assert Path(home) == source_home.parent
    assert grok_home == "unset"
    assert "--reasoning-effort high" in args
    assert "--no-memory" in args
    assert "--prompt-file" in args
    assert Path(uv_cache) == clone / ".grok-output/.runtime-cache/uv"
    assert Path(shared_venvs) == tmp_roots["shared"] / "venvs"
    assert mcp_state == "present"
    prompt = (clone / ".grok-worker/prompt-one-shot.md").read_text(encoding="utf-8")
    assert prompt.startswith("# Grok Worker Stable Base v1")
    assert "Dependency preparation is disabled" in prompt
    assert "Do not run uv, uv run, uv sync, pip" in prompt
    assert str(clone) not in prompt
    assert str(tmp_roots["shared"]) not in prompt

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
    monkeypatch.setenv("HOME", str(source_home.parent))

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


def test_environment_check_failure_warns_but_does_not_block_launch(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_grok(tmp_roots["root"] / "bin", inspect_exit=9)
    source_home = _source_home(tmp_roots["root"])
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", str(fake))
    monkeypatch.setenv("HOME", str(source_home.parent))

    outcome = run_worker(
        RunConfig(
            source=git_source,
            prompt="implement and verify",
            disposable_root=tmp_roots["disposable"],
            artifact_root=tmp_roots["artifacts"],
            shared_cache_root=tmp_roots["shared"],
            backend="native",
            prepare_deps=False,
            task_id="native-preflight-warning",
            keep_reason="inspect warning log",
            skip_post_gc=True,
        )
    )

    assert outcome.exit_code == 0
    worker_log = Path(outcome.artifact_path or "") / "worker.log"
    assert "environment check exited 9" in worker_log.read_text(encoding="utf-8")


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
        backend="acp",
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


def test_native_continuation_reuses_clone_then_closes_cleanly(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_grok(tmp_roots["root"] / "bin")
    source_home = _source_home(tmp_roots["root"])
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", str(fake))
    monkeypatch.setenv("HOME", str(source_home.parent))
    common = dict(
        source=git_source,
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        backend="native",
        prepare_deps=False,
        task_id="native-continue",
        skip_post_gc=True,
    )

    first = run_worker(
        RunConfig(
            **common,
            prompt="make the first bounded change",
            keep_reason="native continuation (TTL-bounded)",
            write_continuation=True,
            run_id="native-turn-one",
        )
    )
    assert first.state == "keep"
    clone = Path(first.clone_path or "")
    assert clone.is_dir()
    lifecycle = json.loads((clone / ".grok-worker/lifecycle.json").read_text(encoding="utf-8"))
    assert lifecycle["retention_deadline"] is not None
    assert (clone / ".grok-worker/continuation.json").is_file()

    second = run_worker(
        RunConfig(
            **common,
            prompt="finish the same task",
            continue_task=True,
            run_id="native-turn-two",
        )
    )
    assert second.state == "success"
    assert second.clone_path is None
    assert not clone.exists()
    assert Path(first.artifact_path or "").is_dir()
    assert Path(second.artifact_path or "").is_dir()
    assert first.artifact_path != second.artifact_path
    receipt = json.loads(
        (Path(second.artifact_path or "") / "verification.txt").read_text(encoding="utf-8")
    )
    assert receipt["metrics"][-1]["continue_session"] is True


def test_expired_native_continuation_is_garbage_collected(
    short_git_source: Path,
    short_tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_source = short_git_source
    tmp_roots = short_tmp_roots
    fake = _fake_grok(tmp_roots["root"] / "bin")
    source_home = _source_home(tmp_roots["root"])
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", str(fake))
    monkeypatch.setenv("HOME", str(source_home.parent))
    outcome = run_worker(
        RunConfig(
            source=git_source,
            prompt="retain briefly",
            disposable_root=tmp_roots["disposable"],
            artifact_root=tmp_roots["artifacts"],
            shared_cache_root=tmp_roots["shared"],
            backend="native",
            prepare_deps=False,
            task_id="native-expire",
            keep_reason="native continuation (TTL-bounded)",
            write_continuation=True,
            skip_post_gc=True,
        )
    )
    clone = Path(outcome.clone_path or "")
    worker = json.loads(
        (Path(outcome.artifact_path or "") / "worker.log").read_text(encoding="utf-8")
    )
    assert worker["session"]["closed"] is False
    assert worker["session"]["retained"] is True
    session_root = clone_session_root(clone)
    session_root.mkdir(parents=True, exist_ok=True)
    (session_root / "session.json").write_text("{}\n", encoding="utf-8")
    meta = WorkerMeta.read(meta_path(clone))
    meta.retention_deadline = dt_to_iso(utc_now() - timedelta(seconds=1))
    meta.write(meta_path(clone))

    report = gc_disposable_root(
        tmp_roots["disposable"],
        protected=[git_source, tmp_roots["artifacts"], tmp_roots["shared"]],
        shared_cache_root=tmp_roots["shared"],
    )
    assert clone.name in report.removed
    assert not clone.exists()
    assert not session_root.exists()


def test_prompt_only_native_continuation_uses_research_contract(
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_grok(tmp_roots["root"] / "bin")
    source_home = _source_home(tmp_roots["root"])
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", str(fake))
    monkeypatch.setenv("HOME", str(source_home.parent))
    common = dict(
        source=None,
        prompt_only=True,
        mode="research",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        backend="native",
        prepare_deps=False,
        task_id="prompt-only-continue",
        skip_post_gc=True,
    )
    first = run_worker(
        RunConfig(
            **common,
            prompt="first research turn",
            keep_reason="native continuation (TTL-bounded)",
            write_continuation=True,
        )
    )
    assert first.state == "keep"
    clone = Path(first.clone_path or "")
    second = run_worker(
        RunConfig(
            **common,
            prompt="finish research",
            continue_task=True,
        )
    )
    assert second.state == "success"
    assert not clone.exists()


def test_semantic_failure_does_not_create_kept_continuation(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_grok(tmp_roots["root"] / "bin")
    fake.write_text(
        fake.read_text(encoding="utf-8").replace(
            '"task_completed":true,"status":"completed"',
            '"task_completed":false,"status":"partial"',
        ),
        encoding="utf-8",
    )
    source_home = _source_home(tmp_roots["root"])
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", str(fake))
    monkeypatch.setenv("HOME", str(source_home.parent))
    outcome = run_worker(
        RunConfig(
            source=git_source,
            prompt="partial result",
            disposable_root=tmp_roots["disposable"],
            artifact_root=tmp_roots["artifacts"],
            shared_cache_root=tmp_roots["shared"],
            backend="native",
            prepare_deps=False,
            task_id="native-partial-continuation",
            keep_reason="native continuation (TTL-bounded)",
            write_continuation=True,
            skip_post_gc=True,
        )
    )
    clone = Path(outcome.clone_path or "")
    assert outcome.state == "failed"
    assert clone.is_dir()
    assert not (clone / ".grok-worker/continuation.json").exists()
    lifecycle = WorkerMeta.read(meta_path(clone))
    assert lifecycle.retention_deadline is not None
