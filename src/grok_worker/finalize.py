"""Terminal-state application, artifact collection helpers, and finalize_run."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from grok_worker.artifacts import ArtifactError, collect_artifacts
from grok_worker.completion_events import emit_completion_event
from grok_worker.deps import detect_clone_local_env
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import default_shared_cache_root, meta_path
from grok_worker.result_schema import (
    ResultError,
    is_task_success,
    load_valid_result,
    result_path,
    validate_verification_files,
    write_captured_analysis_result,
)
from grok_worker.run_config import RunConfig, RunOutcome
from grok_worker.safety import SafetyError, safe_rmtree

# Bounded classifier for recognizable upstream ACP runtime/transport failures.
# Matches only short structured acpx error lines; never copies arbitrary model output.
_ACP_ERROR_RE = re.compile(
    r"\[acpx\]\s*error:\s*((?:RUNTIME|TRANSPORT)\s+[^\n\r]{1,80})",
    re.IGNORECASE,
)
_AGENT_OUTPUT_SCAN_LIMIT = 2048


def summarize_acp_failure(agent_output: str) -> str | None:
    """Return a short safe summary of a recognizable ACP runtime/transport failure.

    Only classifies bounded, structured ``[acpx] error: ...`` lines. Arbitrary or
    long model output is never copied into lifecycle fields.
    """
    if not agent_output:
        return None
    snippet = agent_output[:_AGENT_OUTPUT_SCAN_LIMIT]
    match = _ACP_ERROR_RE.search(snippet)
    if match is None:
        return None
    detail = " ".join(match.group(1).split())
    if not detail:
        return None
    return f"upstream ACP failure: {detail}"


def _compose_result_error_message(exc: BaseException, agent_log: Path | None) -> str:
    """Preserve structured-result failure and surface recognizable ACP failures."""
    structured = str(exc)
    if agent_log is None or not agent_log.is_file() or agent_log.is_symlink():
        return structured
    try:
        agent_output = agent_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return structured
    acp_summary = summarize_acp_failure(agent_output)
    if not acp_summary:
        return structured
    return f"{structured}; {acp_summary}"


def _notify_terminal(
    meta: WorkerMeta,
    *,
    shared_cache_root: Path | None = None,
) -> None:
    """Append a deduplicated completion notification for a terminal state."""
    if meta.state not in (
        WorkerState.SUCCESS,
        WorkerState.FAILED,
        WorkerState.KEEP,
        WorkerState.LEGACY_IMPORTED,
    ):
        return
    root = shared_cache_root
    emit_completion_event(
        task_id=meta.task_id,
        state=str(meta.state),
        artifact_path=meta.artifact_path,
        shared_cache_root=root,
        timestamp=meta.updated_at or None,
    )


def mark_failed(
    meta: WorkerMeta,
    clone: Path,
    *,
    retain_hours: int,
    message: str,
    exit_code: int = 1,
    interrupted: bool = False,
    shared_cache_root: Path | None = None,
) -> None:
    now = utc_now()
    meta.state = WorkerState.FAILED
    meta.exit_code = exit_code
    meta.error_message = message
    meta.retention_deadline = dt_to_iso(now + timedelta(hours=retain_hours))
    meta.artifact_complete = False
    meta.interrupted = interrupted
    meta.runner_pid = None
    meta.runner_start_token = None
    meta.acpx_pid = None
    meta.acpx_start_token = None
    meta.pid = None
    meta.touch()
    meta.write(meta_path(clone))
    _notify_terminal(meta, shared_cache_root=shared_cache_root)


def try_collect(
    clone: Path,
    meta: WorkerMeta,
    artifacts: Path,
    disposable: Path,
    agent_log: Path | None,
    audit: dict[str, object] | None = None,
) -> Path | None:
    try:
        return collect_artifacts(
            clone,
            meta,
            artifacts,
            agent_log=agent_log,
            disposable_root=disposable,
            audit=audit,
        )
    except Exception:  # noqa: BLE001
        return None


def apply_terminal_in_memory(
    meta: WorkerMeta,
    *,
    success: bool,
    acpx_exit: int,
    keep_reason: str | None,
    retain_hours: int,
) -> None:
    """Set terminal fields on *meta* in memory only (not yet success-persisted)."""
    now = utc_now()
    if keep_reason is not None:
        if not keep_reason.strip():
            raise ValueError("--keep reason must be nonempty")
        meta.state = WorkerState.KEEP
        meta.keep_reason = keep_reason.strip()
        meta.exit_code = 0 if success else 1
        meta.retention_deadline = None
        meta.artifact_complete = True
    elif success:
        meta.state = WorkerState.SUCCESS
        meta.exit_code = 0
        meta.retention_deadline = None
        meta.artifact_complete = True
    else:
        meta.state = WorkerState.FAILED
        meta.exit_code = 1 if acpx_exit == 0 else acpx_exit
        meta.retention_deadline = dt_to_iso(now + timedelta(hours=retain_hours))
        meta.artifact_complete = True
        if not meta.error_message:
            meta.error_message = "semantic or transport failure"
    meta.runner_pid = None
    meta.runner_start_token = None
    meta.acpx_pid = None
    meta.acpx_start_token = None
    meta.pid = None
    meta.touch()


def finalize_run(
    cfg: RunConfig,
    clone: Path,
    meta: WorkerMeta,
    disposable: Path,
    artifacts: Path,
    protected: list[Path],
    agent_log: Path | None,
    acpx_exit: int,
    audit: dict[str, object] | None = None,
) -> RunOutcome:
    task_id = meta.task_id
    shared = (cfg.shared_cache_root or default_shared_cache_root()).resolve()
    meta.state = WorkerState.FINALIZING
    meta.touch()
    meta.write(meta_path(clone))

    local_envs = detect_clone_local_env(clone)
    if local_envs:
        mark_failed(
            meta,
            clone,
            retain_hours=cfg.failure_retain_hours,
            message=f"clone-local Python env detected: {local_envs}",
            shared_cache_root=shared,
        )
        art = try_collect(clone, meta, artifacts, disposable, agent_log, audit)
        return RunOutcome(
            task_id=task_id,
            state=str(meta.state),
            exit_code=1,
            clone_path=str(clone),
            artifact_path=str(art) if art else None,
            message=meta.error_message or "local env",
        )

    result = None
    if cfg.mode == "analysis" and acpx_exit == 0 and not result_path(clone).exists():
        try:
            write_captured_analysis_result(clone, agent_log)
        except (ResultError, OSError, ValueError) as exc:
            meta.error_message = _compose_result_error_message(exc, agent_log)
    try:
        result = load_valid_result(clone)
        validate_verification_files(clone, result)
        meta.result_status = str(result.status)
    except (ResultError, OSError, ValueError) as exc:
        # Keep structured-result failure; also surface recognizable ACP runtime errors.
        meta.error_message = _compose_result_error_message(exc, agent_log)
        result = None  # invalid/unverified result must not count as success

    success = is_task_success(acpx_exit, result, mode=cfg.mode)
    try:
        apply_terminal_in_memory(
            meta,
            success=success,
            acpx_exit=acpx_exit,
            keep_reason=cfg.keep_reason,
            retain_hours=cfg.failure_retain_hours,
        )
    except ValueError as exc:
        mark_failed(
            meta,
            clone,
            retain_hours=cfg.failure_retain_hours,
            message=str(exc),
            shared_cache_root=shared,
        )
        return RunOutcome(
            task_id=task_id,
            state=str(meta.state),
            exit_code=1,
            clone_path=str(clone),
            artifact_path=None,
            message=str(exc),
        )

    disk_state = meta.state
    meta.state = WorkerState.FINALIZING
    meta.artifact_complete = False
    meta.write(meta_path(clone))
    meta.state = disk_state
    meta.artifact_complete = True

    if audit is not None:
        # One-shot runs may pass metrics-only audit (no "session" key). Missing
        # session means the truthful one-shot default: already closed. Explicit
        # named-session audit must still fail closed unless closed is True.
        if "session" not in audit:
            session_closed = True
        else:
            session_audit = audit.get("session")
            session_closed = (
                isinstance(session_audit, dict) and session_audit.get("closed") is True
            )
        audit["cleanup_receipt"] = {
            "cloneDeletionAuthorized": meta.state == WorkerState.SUCCESS,
            "sessionClosed": session_closed,
            "requestedState": str(meta.state),
        }

    try:
        art = collect_artifacts(
            clone,
            meta,
            artifacts,
            agent_log=agent_log,
            disposable_root=disposable,
            audit=audit,
        )
    except (ArtifactError, OSError, ValueError) as exc:
        mark_failed(
            meta,
            clone,
            retain_hours=cfg.failure_retain_hours,
            message=f"artifact finalization failed: {exc}",
            exit_code=1 if acpx_exit == 0 else acpx_exit,
            shared_cache_root=shared,
        )
        return RunOutcome(
            task_id=task_id,
            state=str(meta.state),
            exit_code=meta.exit_code or 1,
            clone_path=str(clone),
            artifact_path=None,
            message=meta.error_message or "artifact failed",
        )

    meta.artifact_path = str(art)
    meta.artifact_complete = True
    meta.write(meta_path(clone))
    _notify_terminal(meta, shared_cache_root=shared)

    if agent_log is not None and agent_log.parent.name == f".run-log-{task_id}":
        try:
            safe_rmtree(
                agent_log.parent,
                disposable_root=artifacts,
                protected=[Path.home(), art, disposable, clone],
            )
        except (SafetyError, OSError):
            pass

    clone_out: str | None = str(clone)
    if meta.state == WorkerState.SUCCESS and meta.artifact_complete:
        try:
            safe_rmtree(clone, disposable_root=disposable, protected=protected)
            clone_out = None
        except SafetyError as exc:
            meta.error_message = f"success but clone delete failed: {exc}"
            meta.write(meta_path(clone))

    ok_states = (WorkerState.SUCCESS, WorkerState.KEEP)
    code = 0 if success and meta.state in ok_states else (meta.exit_code or 1)
    return RunOutcome(
        task_id=task_id,
        state=str(meta.state),
        exit_code=code,
        clone_path=clone_out,
        artifact_path=str(art),
        message="ok" if success else (meta.error_message or "task failed"),
    )
