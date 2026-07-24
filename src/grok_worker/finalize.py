"""Terminal-state application, artifact collection helpers, and finalize_run."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from grok_worker.artifacts import ArtifactError, collect_artifacts
from grok_worker.completion_events import emit_completion_event, list_completion_events
from grok_worker.continuation import DEFAULT_CONTINUATION_TTL_HOURS
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
_AGENT_OUTPUT_TAIL_LIMIT = 8192
_STARTUP_ERROR_RE = re.compile(r"\[grok-worker\]\s*startup failed:\s*([^\n\r]{1,160})")
_PROVIDER_HTTP_STATUS_RE = re.compile(
    r"(?:responses\s+api\s+error|api\s+error)[^\n\r]{0,240}?"
    r"\bstatus\s*[=:]?\s*(401|403|429|5\d\d)\b",
    re.IGNORECASE,
)
_LIVE_PROVIDER_HTTP_STATUS_RE = re.compile(
    r"responses\s+api\s+error[^\n\r]{0,240}?"
    r"\bstatus\s*[=:]?\s*(401|403|429|5\d\d)\b",
    re.IGNORECASE,
)
_PROVIDER_JSON_STATUS_RE = re.compile(
    r'"http_status"\s*:\s*(401|403|429|5\d\d)\b',
    re.IGNORECASE,
)
_PROVIDER_UNAVAILABLE_RE = re.compile(
    r"service temporarily unavailable|the model did not respond to this request",
    re.IGNORECASE,
)
_RUNTIME_ERROR_ENVELOPE_RE = re.compile(
    r"responses\s+api\s+error|\{\s*\"type\"\s*:\s*\"error\"|error:\s*internal error",
    re.IGNORECASE,
)
_REASONING_DOWNGRADE_RE = re.compile(
    r"model does not support reasoning effort; ignoring",
    re.IGNORECASE,
)
_MAX_TOKENS_RE = re.compile(
    r'"error_kind"\s*:\s*"max_tokens_truncation"|\bmax_tokens_truncation\b',
    re.IGNORECASE,
)
_MAX_TURNS_RE = re.compile(
    r'(?:"type"\s*:\s*"max_turns_reached"|^\s*Error:\s*max turns reached\s*$)',
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class BackendFailure:
    """Non-sensitive failure classification owned by the lifecycle runner."""

    kind: str
    summary: str
    continuation_safe: bool = False


def _bounded_failure_text(agent_output: str) -> str:
    if len(agent_output) <= _AGENT_OUTPUT_SCAN_LIMIT + _AGENT_OUTPUT_TAIL_LIMIT:
        return agent_output
    return agent_output[:_AGENT_OUTPUT_SCAN_LIMIT] + "\n" + agent_output[-_AGENT_OUTPUT_TAIL_LIMIT:]


def _provider_status(text: str, *, live_only: bool = False) -> int | None:
    patterns = (
        (_LIVE_PROVIDER_HTTP_STATUS_RE, _PROVIDER_JSON_STATUS_RE)
        if live_only
        else (_PROVIDER_HTTP_STATUS_RE, _PROVIDER_JSON_STATUS_RE)
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match is not None:
            return int(match.group(1))
    return None


def classify_live_backend_attention(agent_output: str) -> str | None:
    """Return a non-sensitive reason code for actionable live backend failures."""
    snippet = _bounded_failure_text(agent_output)
    status = _provider_status(snippet, live_only=True)
    if status in (401, 403):
        return "provider_auth_rejected"
    if status == 429:
        return "provider_rate_limited"
    if status is not None and 500 <= status <= 599:
        return "provider_http_5xx"
    if _PROVIDER_UNAVAILABLE_RE.search(snippet) and _RUNTIME_ERROR_ENVELOPE_RE.search(snippet):
        return "provider_unavailable"
    if _REASONING_DOWNGRADE_RE.search(snippet):
        return "reasoning_effort_ignored"
    if _ACP_ERROR_RE.search(snippet):
        return "backend_transport_error"
    return None


def summarize_acp_failure(agent_output: str) -> str | None:
    """Return a short safe summary of a recognizable ACP runtime/transport failure.

    Only classifies bounded, structured ``[acpx] error: ...`` lines. Arbitrary or
    long model output is never copied into lifecycle fields.
    """
    if not agent_output:
        return None
    snippet = _bounded_failure_text(agent_output)
    match = _ACP_ERROR_RE.search(snippet)
    if match is None:
        return None
    detail = " ".join(match.group(1).split())
    if not detail:
        return None
    return f"upstream ACP failure: {detail}"


def classify_backend_failure(agent_output: str) -> BackendFailure | None:
    """Classify bounded structured backend failures in priority order."""
    if not agent_output:
        return None
    snippet = _bounded_failure_text(agent_output)
    if _MAX_TOKENS_RE.search(snippet):
        return BackendFailure(
            kind="max_tokens_truncation",
            summary="upstream native failure: response truncated by max_tokens",
            continuation_safe=True,
        )
    if _MAX_TURNS_RE.search(snippet):
        return BackendFailure(
            kind="max_turns_reached",
            summary="upstream native failure: max turns reached",
            continuation_safe=True,
        )
    acp = summarize_acp_failure(agent_output)
    if acp:
        return BackendFailure(kind="backend_transport_error", summary=acp)
    startup = _STARTUP_ERROR_RE.search(snippet)
    if startup:
        return BackendFailure(
            kind="backend_startup_failure",
            summary=f"backend startup failure: {' '.join(startup.group(1).split())}",
        )
    status = _provider_status(snippet)
    if status is not None:
        return BackendFailure(
            kind=f"provider_http_{status}",
            summary=f"upstream provider failure: HTTP {status}",
        )
    if _PROVIDER_UNAVAILABLE_RE.search(snippet):
        return BackendFailure(
            kind="provider_unavailable",
            summary="upstream provider failure: service unavailable",
        )
    for line in snippet.splitlines():
        try:
            payload = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("type") != "error":
            continue
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            detail = " ".join(message.split())[:160]
            return BackendFailure(
                kind="native_backend_error",
                summary=f"upstream native failure: {detail}",
            )
    return None


def summarize_backend_failure(agent_output: str) -> str | None:
    """Return the safe summary for a bounded backend failure."""
    failure = classify_backend_failure(agent_output)
    return failure.summary if failure is not None else None


def _compose_result_error_message(exc: BaseException, agent_log: Path | None) -> str:
    """Preserve structured-result failure and surface recognizable ACP failures."""
    structured = str(exc)
    if agent_log is None or not agent_log.is_file() or agent_log.is_symlink():
        return structured
    try:
        agent_output = agent_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return structured
    backend_summary = summarize_backend_failure(agent_output)
    if not backend_summary:
        return structured
    return f"{backend_summary}; secondary result contract failure: {structured}"


def _compose_artifact_error_message(primary: str | None, exc: BaseException) -> str:
    secondary = f"artifact finalization failed: {exc}"
    if not primary:
        return secondary
    if secondary in primary:
        return primary
    return f"{primary}; secondary {secondary}"


def _notify_terminal(
    meta: WorkerMeta,
    *,
    shared_cache_root: Path | None = None,
) -> bool:
    """Append a deduplicated completion notification for a terminal state."""
    if meta.state not in (
        WorkerState.SUCCESS,
        WorkerState.FAILED,
        WorkerState.KEEP,
        WorkerState.LEGACY_IMPORTED,
    ):
        return False
    root = shared_cache_root
    event = emit_completion_event(
        task_id=meta.task_id,
        state=str(meta.state),
        artifact_path=meta.artifact_path,
        shared_cache_root=root,
        timestamp=meta.updated_at or None,
        run_id=meta.run_id,
        dispatcher_id=meta.dispatcher_id,
    )
    ready = event is not None
    if not ready and meta.run_id:
        existing = list_completion_events(
            shared_cache_root=root,
            run_id=meta.run_id,
            wait_seconds=0,
        )
        ready = any(
            item.get("state") == str(meta.state)
            and item.get("kind", "terminal") == "terminal"
            for item in existing
        )
    meta.terminal_event_ready = ready
    try:
        meta.write(meta_path(Path(meta.clone_realpath)))
    except OSError:
        pass
    return meta.terminal_event_ready


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
    keep_ttl_hours: float | None = None,
) -> None:
    """Set terminal fields on *meta* in memory only (not yet success-persisted)."""
    now = utc_now()
    if keep_reason is not None:
        if not keep_reason.strip():
            raise ValueError("--keep reason must be nonempty")
        meta.state = WorkerState.KEEP
        meta.keep_reason = keep_reason.strip()
        meta.exit_code = 0 if success else 1
        meta.retention_deadline = (
            dt_to_iso(now + timedelta(hours=keep_ttl_hours)) if keep_ttl_hours is not None else None
        )
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
            run_id=meta.run_id,
            dispatcher_id=meta.dispatcher_id,
        )

    result = None
    analysis_like = cfg.mode in ("analysis", "research") or cfg.prompt_only
    if analysis_like and acpx_exit == 0 and not result_path(clone).exists():
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
        # Preserve a prior authoritative runner message (e.g. reasoning-effort ignored).
        composed = _compose_result_error_message(exc, agent_log)
        if meta.error_message:
            secondary = f"secondary result contract failure: {exc}"
            if secondary not in meta.error_message:
                meta.error_message = f"{meta.error_message}; {secondary}"
        else:
            meta.error_message = composed
        result = None  # invalid/unverified result must not count as success

    # Prompt-only / research never synthesizes implementation success.
    success_mode = "analysis" if analysis_like else cfg.mode
    success = is_task_success(acpx_exit, result, mode=success_mode)
    try:
        apply_terminal_in_memory(
            meta,
            success=success,
            acpx_exit=acpx_exit,
            keep_reason=cfg.keep_reason,
            retain_hours=cfg.failure_retain_hours,
            keep_ttl_hours=(
                DEFAULT_CONTINUATION_TTL_HOURS
                if cfg.write_continuation and cfg.backend == "native"
                else None
            ),
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
            run_id=meta.run_id,
            dispatcher_id=meta.dispatcher_id,
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
            session_closed = isinstance(session_audit, dict) and session_audit.get("closed") is True
        audit["cleanup_receipt"] = {
            "cloneDeletionAuthorized": (
                meta.state == WorkerState.SUCCESS
                or (
                    meta.state == WorkerState.KEEP
                    and meta.retention_deadline is not None
                    and cfg.write_continuation
                )
            ),
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
            message=_compose_artifact_error_message(meta.error_message, exc),
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
            run_id=meta.run_id,
            dispatcher_id=meta.dispatcher_id,
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
    if (
        meta.state == WorkerState.SUCCESS
        and meta.artifact_complete
        and meta.terminal_event_ready
    ):
        try:
            safe_rmtree(clone, disposable_root=disposable, protected=protected)
            clone_out = None
        except SafetyError as exc:
            meta.error_message = f"success but clone delete failed: {exc}"
            meta.write(meta_path(clone))
    elif meta.state == WorkerState.SUCCESS and not meta.terminal_event_ready:
        meta.error_message = "success retained because terminal notification was not durable"
        meta.write(meta_path(clone))

    ok_states = (WorkerState.SUCCESS, WorkerState.KEEP)
    code = 0 if success and meta.state in ok_states else (meta.exit_code or 1)
    return RunOutcome(
        task_id=task_id,
        state=str(meta.state),
        exit_code=code,
        clone_path=clone_out,
        artifact_path=str(art),
        message=(
            "ok"
            if success and meta.terminal_event_ready
            else (meta.error_message or "task failed")
        ),
        run_id=meta.run_id,
        dispatcher_id=meta.dispatcher_id,
    )
