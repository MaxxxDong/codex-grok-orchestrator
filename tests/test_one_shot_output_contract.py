"""One-shot runs inject Skill-owned base + role + exact output contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
TASK_IMPL = "Implement widget validation with a focused regression test."
TASK_ANALYSIS = "Review the auth module for unsafe path handling."


def _assert_base_and_task(prompt: str, task: str) -> None:
    assert "Grok Worker Stable Base v1" in prompt
    assert "configured worker profile" in prompt.lower()
    assert "do not switch" in prompt.lower()
    assert task in prompt
    assert "GROK_ONE_SHOT_TASK" in prompt


def _assert_implementation_contract(prompt: str) -> None:
    assert "Role: implement" in prompt
    assert ".grok-output/result.json" in prompt
    assert ".grok-output/verification/" in prompt
    for key in (
        "schema_version",
        "task_completed",
        "status",
        "summary",
        "findings",
        "verification",
        "command",
        "exit_code",
        "log_path",
    ):
        assert key in prompt, f"missing required contract key mention: {key}"
    completed_markers = (
        "task_completed=true",
        'task_completed": true',
        "task_completed: true",
    )
    assert any(marker in prompt for marker in completed_markers)
    status_markers = (
        'status="completed"',
        'status: "completed"',
        "status completed",
    )
    assert any(marker in prompt or marker in prompt.lower() for marker in status_markers)
    lower = prompt.lower()
    write_rules = (
        "without creating files is failure",
        "without creating required files is failure",
        "chat-only",
        "printing/chatting",
        "printing or chatting",
        "do not merely print",
        "write-not-print",
        "writing the files is mandatory",
        "writing the verification logs",
        "not sufficient to print",
        "not merely print",
    )
    assert any(rule in lower for rule in write_rules), (
        "write-not-print / files-required rule missing from implement contract"
    )
    findings_shape_markers = (
        "each findings entry must be a json object",
        "every findings entry must be a json object",
        "every nonempty findings entry must be a json object",
        "findings entries must be json objects",
    )
    assert any(marker in lower for marker in findings_shape_markers), (
        "implementation contract must define nonempty findings entries as JSON objects"
    )
    assert ".grok-worker/progress.json" in prompt
    assert all(step in lower for step in ("planning", "editing", "verifying", "finalizing"))
    assert "task_completed" in lower and "false" in lower
    assert 'status": "partial"' in lower or 'status: "partial"' in lower
    assert "atomic" in lower and ("rename" in lower or "replace" in lower)
    assert "before extensive" in lower or "before editing" in lower
    # 0.7: native structured-output path vs ACP/legacy disk path
    assert "NATIVE_STRUCTURED_RESULT_CAPTURE" in prompt or "native structured-output" in lower
    assert "acp" in lower or "legacy" in lower


def test_one_shot_implementation_prompt_includes_base_role_and_output_contract() -> None:
    from grok_worker.prompt_cache import build_one_shot_prompt

    prompt = build_one_shot_prompt(SKILL_ROOT, "implementation", TASK_IMPL)
    _assert_base_and_task(prompt, TASK_IMPL)
    _assert_implementation_contract(prompt)


def test_stable_prompt_forbids_disposable_paths_in_project_artifacts() -> None:
    """Workers must not persist clone cwd as the canonical repository path."""
    from grok_worker.prompt_cache import build_one_shot_prompt

    prompt = build_one_shot_prompt(SKILL_ROOT, "implementation", TASK_IMPL)

    assert ".grok-worker/lifecycle.json" in prompt
    assert "source_realpath" in prompt
    assert "Never write the disposable clone path" in prompt


def test_stable_prompt_includes_execution_efficiency_rules() -> None:
    from grok_worker.prompt_cache import build_one_shot_prompt

    prompt = build_one_shot_prompt(SKILL_ROOT, "implementation", TASK_IMPL)
    lower = prompt.lower()
    assert "execution efficiency" in lower
    assert "targeted" in lower or "targeted reads" in lower
    assert "smallest relevant" in lower
    assert "full suite" in lower
    assert "clone-local" in lower or "clone-local `.venv`" in prompt
    assert "never create a clone-local" in lower or "never create a" in lower
    assert "subagents" in lower
    assert "independent" in lower


def test_debug_role_prompt_defines_findings_object_shape() -> None:
    from grok_worker.prompt_cache import Role, _load_base_and_role

    prompt = _load_base_and_role(SKILL_ROOT, Role.DEBUG)
    assert "Role: debug" in prompt
    _assert_implementation_contract(prompt.replace("Role: debug", "Role: implement"))


def _assert_no_terminal_analysis_guidance(prompt: str) -> None:
    """Read-only analysis/review/research must never attempt shell under --no-terminal."""
    lower = prompt.lower()
    no_terminal_markers = (
        "no-terminal",
        "no terminal",
        "without terminal",
        "terminal unavailable",
        "terminal is unavailable",
        "do not use terminal",
        "do not run terminal",
        "never use terminal",
        "never run terminal",
        "do not use shell",
        "do not run shell",
        "never use shell",
        "never run shell",
        "no shell",
        "without shell",
        "shell unavailable",
        "do not execute shell",
        "do not execute terminal",
        "do not run commands",
        "never run commands",
        "no command execution",
        "do not attempt terminal",
        "do not attempt shell",
        "terminal/shell",
        "shell/terminal",
    )
    assert any(marker in lower for marker in no_terminal_markers), (
        "analysis/read-only role prompt missing explicit no-terminal/no-shell guidance"
    )
    read_search_markers = (
        "read/search",
        "read and search",
        "workspace read",
        "read tools",
        "search tools",
        "read/search tools",
        "workspace read/search",
        "file read",
        "search capabilities",
        "read and search tools",
        "read/search capabilities",
    )
    assert any(marker in lower for marker in read_search_markers), (
        "analysis/read-only role prompt missing direction to use read/search tools"
    )
    report_limitation_markers = (
        "report the limitation",
        "report limitations",
        "report a limitation",
        "report that limitation",
        "rather than request permission",
        "instead of requesting permission",
        "do not request permission",
        "without requesting permission",
        "never request permission",
        "report the limitation instead",
    )
    assert any(marker in lower for marker in report_limitation_markers), (
        "analysis/read-only role prompt missing guidance to report limitations "
        "rather than request permission"
    )


def test_one_shot_analysis_prompt_is_readonly_review_without_write_contract() -> None:
    from grok_worker.prompt_cache import build_one_shot_prompt

    prompt = build_one_shot_prompt(SKILL_ROOT, "analysis", TASK_ANALYSIS)
    _assert_base_and_task(prompt, TASK_ANALYSIS)
    assert "Role: review" in prompt
    assert "read-only" in prompt.lower() or "Remain read-only" in prompt
    lower = prompt.lower()
    assert "write `.grok-output/result.json`" not in lower
    assert "write .grok-output/result.json" not in lower
    review_tail = prompt.split("Role: review")[-1].lower()
    if "you must write" in lower:
        assert "result.json" not in review_tail
    assert "Role: implement" not in prompt
    assert "Role: debug" not in prompt
    _assert_no_terminal_analysis_guidance(prompt)


def test_one_shot_analysis_prompt_honors_no_terminal_mode() -> None:
    """One-shot --mode analysis must inject explicit no-terminal guidance for ACP."""
    from grok_worker.prompt_cache import build_one_shot_prompt

    prompt = build_one_shot_prompt(SKILL_ROOT, "analysis", TASK_ANALYSIS)
    assert "Role: review" in prompt
    _assert_no_terminal_analysis_guidance(prompt)


def test_research_role_prompt_honors_no_terminal_mode() -> None:
    """Session research role shares the same no-terminal analysis contract."""
    from grok_worker.prompt_cache import Role, _load_base_and_role

    prompt = _load_base_and_role(SKILL_ROOT, Role.RESEARCH)
    assert "Role: research" in prompt
    assert "read-only" in prompt.lower() or "Remain read-only" in prompt
    _assert_no_terminal_analysis_guidance(prompt)


def test_one_shot_prompt_rejects_unsupported_mode() -> None:
    from grok_worker.prompt_cache import build_one_shot_prompt

    with pytest.raises((ValueError, KeyError)):
        build_one_shot_prompt(SKILL_ROOT, "not-a-real-mode", TASK_IMPL)


def test_one_shot_research_mode_is_prompt_only_readonly() -> None:
    from grok_worker.prompt_cache import build_one_shot_prompt

    prompt = build_one_shot_prompt(SKILL_ROOT, "research", "Investigate X")
    assert "Role: research" in prompt
    assert "Role: implement" not in prompt
    _assert_no_terminal_analysis_guidance(prompt)


def test_execute_worker_passes_injected_one_shot_prompt_to_acp(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt handed to acpx must be Skill-built, not the raw caller prompt."""
    from grok_worker import worker_exec
    from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
    from grok_worker.run_config import RunConfig, RunOutcome

    captured: dict[str, Any] = {}

    def fake_popen(cmd: list[str], *args: Any, **kwargs: Any) -> MagicMock:
        assert cmd[-2] == "exec"
        captured["prompt"] = cmd[-1]
        proc = MagicMock()
        proc.pid = 4242
        proc.poll.return_value = 0
        proc.wait.return_value = 0
        return proc

    def fake_finalize(*_args: Any, **_kwargs: Any) -> RunOutcome:
        return RunOutcome(
            task_id="oneshot-contract",
            state="success",
            exit_code=0,
            clone_path=None,
            artifact_path=None,
            message="ok",
        )

    monkeypatch.setattr(worker_exec.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(worker_exec, "finalize_run", fake_finalize)
    monkeypatch.setattr(worker_exec, "capture_identity", lambda: (1, "tok"))
    monkeypatch.setattr(worker_exec, "process_start_token", lambda _pid: "acp-tok")
    monkeypatch.setattr(worker_exec, "gc_disposable_root", lambda *_args, **_kwargs: None)

    class _Lease:
        def acquire(self) -> None:
            return None

        def release(self) -> None:
            return None

    monkeypatch.setattr(worker_exec, "worker_lock", lambda _path: _Lease())
    monkeypatch.setattr(worker_exec, "cache_use_lease", lambda _path: _Lease())
    monkeypatch.setattr(worker_exec, "shared_cache_environment", lambda _path: {})

    clone = tmp_roots["disposable"] / "clone"
    clone.mkdir()
    (clone / ".grok-worker").mkdir()
    now = dt_to_iso(utc_now()) or ""
    meta = WorkerMeta(
        schema_version=1,
        task_id="oneshot-contract",
        source_realpath=str(git_source),
        clone_realpath=str(clone),
        state=WorkerState.CREATING,
        created_at=now,
        updated_at=now,
        managed_by="grok-worker-lifecycle",
    )
    cfg = RunConfig(
        source=git_source,
        prompt=TASK_IMPL,
        backend="acp",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        prepare_deps=False,
        skip_post_gc=True,
        task_id="oneshot-contract",
        mode="implementation",
        acpx_bin="acpx",
    )
    outcome = worker_exec.execute_worker(
        cfg,
        clone,
        meta,
        tmp_roots["disposable"],
        tmp_roots["artifacts"],
        tmp_roots["shared"],
        [git_source],
        "agent",
    )
    assert outcome.exit_code == 0
    assert "prompt" in captured
    prompt = captured["prompt"]
    _assert_base_and_task(prompt, TASK_IMPL)
    _assert_implementation_contract(prompt)
