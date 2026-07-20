"""Garbage collection for lifecycle-managed clones and dead-state conversion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from grok_worker.artifacts import (
    artifact_authorizes_clone_deletion,
    artifact_intends_clone_deletion,
    artifacts_complete_and_verified,
)
from grok_worker.completion_events import emit_completion_event
from grok_worker.constants import DEFAULT_FAILURE_RETAIN_HOURS
from grok_worker.continuation import continuation_path
from grok_worker.grok_state import cleanup_clone_session_state, clone_session_root
from grok_worker.locks import root_lock
from grok_worker.models import (
    WorkerMeta,
    WorkerState,
    dt_from_iso,
    dt_to_iso,
    meta_is_trusted,
    utc_now,
)
from grok_worker.paths import default_shared_cache_root, is_managed_clone, meta_dir, meta_path
from grok_worker.process_identity import process_matches
from grok_worker.safety import SafetyError, safe_rmtree
from grok_worker.temp_cleanup import clean_stale_tmp


@dataclass
class GCReport:
    removed: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    converted_dead: list[str] = field(default_factory=list)
    skipped_legacy: list[str] = field(default_factory=list)
    skipped_untrusted: list[str] = field(default_factory=list)
    tmp_removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _deadline_expired(meta: WorkerMeta, now: datetime) -> bool:
    if not meta.retention_deadline:
        return False
    dl = dt_from_iso(meta.retention_deadline)
    if dl is None:
        return False
    if dl.tzinfo is None:
        dl = dl.replace(tzinfo=UTC)
    return now >= dl


def _expired_continuation(meta: WorkerMeta, clone: Path, now: datetime) -> bool:
    continuation = continuation_path(clone)
    return bool(
        meta.state == WorkerState.KEEP
        and meta.retention_deadline
        and continuation.is_file()
        and not continuation.is_symlink()
        and _deadline_expired(meta, now)
    )


def _worker_lock_held(clone: Path) -> bool:
    import fcntl
    import os

    lock_file = meta_dir(clone) / "worker.lock"
    if not lock_file.exists():
        return False
    fd = os.open(str(lock_file), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def convert_dead_worker(
    meta: WorkerMeta,
    clone: Path,
    *,
    retain_hours: int = DEFAULT_FAILURE_RETAIN_HOURS,
    reason: str = "worker process died",
    shared_cache_root: Path | None = None,
) -> WorkerMeta:
    """Convert dead creating/running/finalizing into failed+24h; never delete.

    Emits at most one completion notification for the failed terminal state
    under the explicit shared cache root (or default when omitted).
    """
    # Already terminal-failed: re-entrancy must not re-notify (dedup also guards).
    already_failed = meta.state == WorkerState.FAILED
    now = utc_now()
    meta.state = WorkerState.FAILED
    meta.runner_pid = None
    meta.runner_start_token = None
    meta.acpx_pid = None
    meta.acpx_start_token = None
    meta.pid = None
    meta.error_message = meta.error_message or reason
    meta.exit_code = meta.exit_code if meta.exit_code is not None else -1
    meta.retention_deadline = dt_to_iso(now + timedelta(hours=retain_hours))
    meta.artifact_complete = False
    meta.touch()
    meta.write(meta_path(clone))
    if not already_failed:
        root = (
            Path(shared_cache_root).resolve()
            if shared_cache_root is not None
            else default_shared_cache_root().resolve()
        )
        emit_completion_event(
            task_id=meta.task_id,
            state=str(meta.state),
            artifact_path=meta.artifact_path,
            shared_cache_root=root,
            timestamp=meta.updated_at or None,
            run_id=meta.run_id,
            dispatcher_id=meta.dispatcher_id,
        )
    return meta


convert_dead_running = convert_dead_worker


def is_active(meta: WorkerMeta, clone: Path) -> bool:
    """Active if lock held, or live process identity matches.

    Idle ``session_open`` does **not** permanently reserve concurrency capacity.
    Max concurrent workers means active Grok invocations/processes only; named
    sessions acquire a transient dispatcher slot around each ACP turn.
    A session_open clone is still treated as non-reclaimable for GC safety
    while the worker lock is held or a live process identity matches.
    """
    if _worker_lock_held(clone):
        return True
    if meta.state not in (
        WorkerState.RUNNING,
        WorkerState.CREATING,
        WorkerState.FINALIZING,
        WorkerState.SESSION_OPEN,
    ):
        return False
    if meta.state == WorkerState.SESSION_OPEN:
        # Protect clone from GC while open; does not count toward capacity budget.
        # Only live process identities or a held worker lock mark it "active".
        if process_matches(meta.runner_pid, meta.runner_start_token):
            return True
        if process_matches(meta.acpx_pid, meta.acpx_start_token):
            return True
        # Still protect idle open sessions from automatic GC deletion.
        return True
    if process_matches(meta.runner_pid, meta.runner_start_token):
        return True
    if process_matches(meta.acpx_pid, meta.acpx_start_token):
        return True
    return False


def should_delete(
    meta: WorkerMeta,
    clone: Path,
    now: datetime,
    *,
    disposable_root: Path,
) -> bool:
    if is_active(meta, clone) or meta.state == WorkerState.FINALIZING:
        return False
    if meta.state == WorkerState.KEEP or meta.keep_reason:
        if _expired_continuation(meta, clone, now):
            return (
                bool(meta.artifact_complete)
                and artifacts_complete_and_verified(
                    meta.artifact_path, clone=clone, disposable_root=disposable_root
                )
                and artifact_intends_clone_deletion(Path(meta.artifact_path or ""))
            )
        return False
    if meta.state == WorkerState.SUCCESS:
        if not meta.artifact_complete:
            return False
        return artifacts_complete_and_verified(
            meta.artifact_path, clone=clone, disposable_root=disposable_root
        ) and artifact_authorizes_clone_deletion(Path(meta.artifact_path or ""))
    if meta.state == WorkerState.FAILED:
        return _deadline_expired(meta, now)
    if meta.state == WorkerState.LEGACY_IMPORTED:
        if meta.legacy_classification == "keep":
            return False
        if meta.legacy_classification == "expire":
            return (
                bool(meta.artifact_complete)
                and artifacts_complete_and_verified(
                    meta.artifact_path, clone=clone, disposable_root=disposable_root
                )
                and artifact_authorizes_clone_deletion(Path(meta.artifact_path or ""))
            )
        if meta.legacy_classification == "retain-24h":
            return bool(meta.artifact_complete) and _deadline_expired(meta, now)
        return False
    return False


def _maybe_convert_dead(
    meta: WorkerMeta,
    clone: Path,
    report: GCReport,
    retain_hours: int,
    *,
    shared_cache_root: Path | None,
) -> WorkerMeta:
    if meta.state not in (
        WorkerState.RUNNING,
        WorkerState.CREATING,
        WorkerState.FINALIZING,
    ):
        return meta
    if is_active(meta, clone):
        return meta
    convert_dead_worker(
        meta,
        clone,
        retain_hours=retain_hours,
        reason=f"dead {meta.state} worker reconciled",
        shared_cache_root=shared_cache_root,
    )
    report.converted_dead.append(clone.name)
    return WorkerMeta.read(meta_path(clone))


def _gc_scan(
    root: Path,
    report: GCReport,
    *,
    prot: list[Path],
    retain_hours: int,
    shared_cache_root: Path | None,
) -> None:
    now = utc_now()
    if not root.is_dir():
        return
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if child.name.startswith("."):
            continue
        if child.is_symlink():
            report.retained.append(f"{child.name}:symlink")
            continue
        if not child.is_dir():
            continue
        if not is_managed_clone(child):
            report.skipped_legacy.append(child.name)
            continue
        try:
            meta = WorkerMeta.read(meta_path(child))
        except (OSError, ValueError, KeyError) as exc:
            report.errors.append(f"{child.name}: bad metadata: {exc}")
            continue

        if not meta_is_trusted(meta):
            report.skipped_untrusted.append(child.name)
            continue

        # clone realpath must match directory being scanned
        try:
            if Path(meta.clone_realpath).resolve() != child.resolve():
                report.skipped_untrusted.append(f"{child.name}:clone_mismatch")
                continue
        except OSError:
            report.skipped_untrusted.append(f"{child.name}:clone_resolve")
            continue

        meta = _maybe_convert_dead(
            meta, child, report, retain_hours, shared_cache_root=shared_cache_root
        )

        if is_active(meta, child):
            report.retained.append(f"{child.name}:active")
            continue

        ok_art = meta.artifact_complete and artifacts_complete_and_verified(
            meta.artifact_path, clone=child, disposable_root=root
        )
        if meta.state == WorkerState.SUCCESS and not ok_art:
            convert_dead_worker(
                meta,
                child,
                retain_hours=retain_hours,
                reason="success without verified external artifacts",
                shared_cache_root=shared_cache_root,
            )
            report.converted_dead.append(child.name)
            report.retained.append(f"{child.name}:artifact_incomplete")
            continue

        if should_delete(meta, child, now, disposable_root=root):
            try:
                if _expired_continuation(meta, child, now):
                    cleanup_clone_session_state(child)
                    session_root = clone_session_root(child)
                    if session_root.exists() or session_root.is_symlink():
                        raise SafetyError(
                            f"retained Grok session cleanup incomplete: {session_root}"
                        )
                safe_rmtree(child, disposable_root=root, protected=prot)
                report.removed.append(child.name)
            except (SafetyError, OSError) as exc:
                report.errors.append(f"{child.name}: {exc}")
        else:
            report.retained.append(f"{child.name}:{meta.state}")


def gc_disposable_root(
    disposable_root: Path,
    *,
    protected: list[Path] | None = None,
    retain_hours: int = DEFAULT_FAILURE_RETAIN_HOURS,
    clean_tmp: bool = True,
    tmp_age_hours: float = 24.0,
    already_locked: bool = False,
    shared_cache_root: Path | None = None,
) -> GCReport:
    report = GCReport()
    root = disposable_root
    root.mkdir(parents=True, exist_ok=True)
    prot = list(protected or [])
    shared = (
        Path(shared_cache_root).resolve()
        if shared_cache_root is not None
        else default_shared_cache_root().resolve()
    )
    if already_locked:
        _gc_scan(
            root,
            report,
            prot=prot,
            retain_hours=retain_hours,
            shared_cache_root=shared,
        )
    else:
        with root_lock(root):
            _gc_scan(
                root,
                report,
                prot=prot,
                retain_hours=retain_hours,
                shared_cache_root=shared,
            )
    if clean_tmp:
        report.tmp_removed = clean_stale_tmp(age_hours=tmp_age_hours, protected=prot)
    return report
