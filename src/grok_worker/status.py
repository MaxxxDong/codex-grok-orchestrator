"""Read-only status of disposable root, caps, states, and shared cache."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from grok_worker.capacity import root_usage_bytes
from grok_worker.constants import (
    DEFAULT_CAP_BYTES,
    EXCLUDE_DIR_NAMES,
    META_DIR_NAME,
    OUTPUT_DIR_NAME,
    RESULT_FILE_NAME,
    VERIFICATION_DIR_NAME,
)
from grok_worker.gc import is_active, should_delete
from grok_worker.models import WorkerMeta, WorkerState, dt_from_iso, meta_is_trusted, utc_now
from grok_worker.paths import default_shared_cache_root, is_managed_clone, meta_dir, meta_path
from grok_worker.resources import empty_resources, process_resources
from grok_worker.safety import dir_size_bytes

# Small tolerance for clock skew between hosts / filesystem mtime vs wall clock.
# Timestamps more than this amount in the future of *now* are ignored so they
# cannot override lifecycle ``updated_at`` or manufacture "recent" activity.
CLOCK_SKEW_TOLERANCE = timedelta(seconds=5)
MAX_ACTIVITY_SCAN_ENTRIES = 20_000
MAX_ACTIVITY_SCAN_DEPTH = 16
PROGRESS_STEPS = frozenset({"planning", "editing", "verifying", "finalizing"})
_ACTIVITY_EXCLUDED_DIRS = (EXCLUDE_DIR_NAMES - {"build", "dist"}) | {
    META_DIR_NAME,
    OUTPUT_DIR_NAME,
}

_TERMINAL_INACTIVE_STATES = frozenset(
    {
        WorkerState.SUCCESS,
        WorkerState.FAILED,
        WorkerState.KEEP,
        WorkerState.LEGACY_IMPORTED,
    }
)

_ACTIVE_ELAPSED_STATES = frozenset(
    {
        WorkerState.CREATING,
        WorkerState.RUNNING,
        WorkerState.FINALIZING,
        WorkerState.SESSION_OPEN,
    }
)


@dataclass
class CloneStatus:
    name: str
    state: str
    size_bytes: int
    reclaimable: bool
    keep_reason: str | None = None
    retention_deadline: str | None = None
    pid: int | None = None
    active: bool = False
    # Per-clone summary (lifecycle-authoritative phase; progress is advisory).
    phase: str = ""
    last_activity_at: str = ""
    activity_source: str = "lifecycle"
    progress_step: str | None = None
    elapsed_seconds: float = 0.0
    timeout_seconds: float | int | None = None
    remaining_seconds: float | int | None = None
    timeout_mode: str = "fixed_legacy"
    hard_timeout_seconds: int | None = None
    hard_remaining_seconds: float | None = None
    lease_revision: int | None = None
    result_ready: bool = False
    artifact_ready: bool = False
    resources: dict[str, float | int | None] = field(default_factory=empty_resources)


@dataclass
class StatusReport:
    disposable_root: str
    usage_bytes: int
    cap_bytes: int
    over_cap: bool
    reclaimable_bytes: int
    shared_cache_root: str
    shared_uv_cache: str
    shared_venvs: str
    shared_locks: str
    clones: list[CloneStatus] = field(default_factory=list)
    unmarked_legacy: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _parse_progress(clone: Path) -> dict[str, Any] | None:
    """Load advisory progress.json; illegal payloads return None (fail-soft)."""
    path = meta_dir(clone) / "progress.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): v for k, v in data.items()}


def _parse_iso_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _safe_iso(value: object) -> str | None:
    dt = _parse_iso_utc(value)
    if dt is None:
        return None
    return dt.isoformat()


def _is_usable_activity_time(dt: datetime, *, now: datetime) -> bool:
    """Reject timestamps that are clearly in the future beyond clock-skew tolerance."""
    return dt <= now + CLOCK_SKEW_TOLERANCE


def _elapsed_between(created_at: str | None, end: datetime) -> float:
    created = dt_from_iso(created_at)
    if created is None:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    delta = (end - created.astimezone(UTC)).total_seconds()
    return max(0.0, float(delta))


def _result_ready(clone: Path) -> bool:
    path = clone / OUTPUT_DIR_NAME / RESULT_FILE_NAME
    return path.is_file() and not path.is_symlink()


def _artifact_ready(meta: WorkerMeta) -> bool:
    if not meta.artifact_complete:
        return False
    if not meta.artifact_path:
        return False
    try:
        art = Path(meta.artifact_path)
    except (TypeError, ValueError):
        return False
    if art.is_symlink() or not art.is_dir():
        # v2 artifacts are directories with three files; also accept verified path existence
        if art.is_file() and not art.is_symlink():
            return True
        return False
    # Directory present is enough for status readiness; full verify is GC concern.
    return True


def _phase_from_lifecycle(meta: WorkerMeta) -> str:
    """Phase is always derived from lifecycle state, never progress claims."""
    return str(meta.state)


def _progress_step(progress: dict[str, Any] | None) -> str | None:
    """Return only the fixed advisory step vocabulary; never arbitrary worker text."""
    if progress is None or progress.get("schema_version") != 1:
        return None
    step = progress.get("step")
    if isinstance(step, str) and step in PROGRESS_STEPS:
        return step
    return None


def _latest_regular_file_mtime(
    root: Path,
    *,
    now: datetime,
    excluded_dirs: frozenset[str] | set[str] = frozenset(),
) -> datetime | None:
    """Bounded, non-symlink scan for observable worker file activity.

    The scan never follows symlinks, ignores managed/cache directories, and is
    capped so status/health remain diagnostics rather than a project crawler.
    """
    if root.is_symlink() or not root.is_dir():
        return None
    latest: datetime | None = None
    seen = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and seen < MAX_ACTIVITY_SCAN_ENTRIES:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > MAX_ACTIVITY_SCAN_ENTRIES:
                        break
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if depth < MAX_ACTIVITY_SCAN_DEPTH and entry.name not in excluded_dirs:
                                stack.append((Path(entry.path), depth + 1))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        stat_result = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    mtime = datetime.fromtimestamp(stat_result.st_mtime, tz=UTC)
                    if not _is_usable_activity_time(mtime, now=now):
                        continue
                    if latest is None or mtime > latest:
                        latest = mtime
        except OSError:
            continue
    return latest


def workspace_activity_at(clone: Path, *, now: datetime) -> datetime | None:
    candidates = [
        _latest_regular_file_mtime(
            clone,
            now=now,
            excluded_dirs=_ACTIVITY_EXCLUDED_DIRS,
        ),
        _latest_regular_file_mtime(
            clone / OUTPUT_DIR_NAME / VERIFICATION_DIR_NAME,
            now=now,
        ),
    ]
    usable = [item for item in candidates if item is not None]
    return max(usable) if usable else None


def _last_activity_at(
    meta: WorkerMeta,
    progress: dict[str, Any] | None,
    clone: Path,
    *,
    now: datetime,
    include_workspace: bool,
) -> tuple[str, str]:
    """Pick the latest usable advisory/lifecycle activity time.

    Lifecycle ``updated_at`` remains the baseline. Progress timestamps and file
    mtimes may only advance activity when they parse cleanly and are not more
    than :data:`CLOCK_SKEW_TOLERANCE` in the future of *now*. Future or
    unparseable values never override lifecycle.
    """
    candidates: list[tuple[datetime, str]] = []
    lifecycle_dt = _parse_iso_utc(meta.updated_at)
    if lifecycle_dt is not None and _is_usable_activity_time(lifecycle_dt, now=now):
        candidates.append((lifecycle_dt, "lifecycle"))

    if progress is not None:
        for key in ("updated_at", "last_activity_at", "timestamp"):
            dt = _parse_iso_utc(progress.get(key))
            if dt is not None and _is_usable_activity_time(dt, now=now):
                candidates.append((dt, "progress"))

    # File mtimes as last-resort advisory activity signal (same future filter).
    for rel in (
        meta_path(clone),
        meta_dir(clone) / "progress.json",
        clone / OUTPUT_DIR_NAME / RESULT_FILE_NAME,
    ):
        try:
            if rel.is_file() and not rel.is_symlink():
                mtime = datetime.fromtimestamp(rel.stat().st_mtime, tz=UTC)
                if _is_usable_activity_time(mtime, now=now):
                    if rel.name == RESULT_FILE_NAME:
                        source = "result"
                    elif rel.name == "progress.json":
                        source = "progress"
                    else:
                        source = "lifecycle"
                    candidates.append((mtime, source))
        except OSError:
            continue

    if include_workspace:
        workspace_dt = workspace_activity_at(clone, now=now)
        if workspace_dt is not None:
            candidates.append((workspace_dt, "workspace"))

    if candidates:
        activity, source = max(candidates, key=lambda item: item[0])
        return activity.isoformat(), source
    # Fallback: lifecycle strings even if unparseable, then created_at.
    return meta.updated_at or meta.created_at or "", "lifecycle"


def _timeout_from_sources(
    meta: WorkerMeta,
    progress: dict[str, Any] | None,
) -> float | int | None:
    timeout: float | int | None = meta.timeout_seconds
    if timeout is None and progress is not None:
        raw = progress.get("timeout_seconds")
        if isinstance(raw, bool):
            return timeout
        if isinstance(raw, int) and raw > 0:
            return raw
        if isinstance(raw, float) and raw > 0:
            return float(raw)
    return timeout


def _timeout_and_remaining(
    meta: WorkerMeta,
    progress: dict[str, Any] | None,
    elapsed: float,
    *,
    terminal: bool,
) -> tuple[float | int | None, float | int | None]:
    timeout = _timeout_from_sources(meta, progress)
    if timeout is None:
        return None, None
    # Terminal clones are frozen: remaining is not a live countdown.
    if terminal:
        return timeout, None
    remaining = max(0.0, float(timeout) - float(elapsed))
    return timeout, remaining


def preferred_resource_pid(meta: WorkerMeta) -> int | None:
    """Resource-sampling PID preference: acpx_pid, then runner_pid, then legacy pid."""
    if meta.acpx_pid is not None and meta.acpx_pid > 0:
        return meta.acpx_pid
    if meta.runner_pid is not None and meta.runner_pid > 0:
        return meta.runner_pid
    if meta.pid is not None and meta.pid > 0:
        return meta.pid
    return None


def _resources_for(meta: WorkerMeta, active: bool) -> dict[str, float | int | None]:
    if not active:
        return empty_resources()
    return process_resources(preferred_resource_pid(meta))


def _is_terminal_inactive(meta: WorkerMeta) -> bool:
    return meta.state in _TERMINAL_INACTIVE_STATES


def _elapsed_for_meta(meta: WorkerMeta, clock: datetime) -> float:
    """Elapsed is frozen for terminal states; live for creating/running/finalizing."""
    if meta.state in _ACTIVE_ELAPSED_STATES:
        return _elapsed_between(meta.created_at, clock)
    if _is_terminal_inactive(meta):
        end = _parse_iso_utc(meta.updated_at)
        if end is None:
            end = clock
        return _elapsed_between(meta.created_at, end)
    # Unknown/other states: use wall clock (fail-open for observability).
    return _elapsed_between(meta.created_at, clock)


def build_clone_summary(
    meta: WorkerMeta,
    clone: Path,
    *,
    now: datetime | None = None,
    active: bool | None = None,
) -> dict[str, Any]:
    """Build per-clone summary fields (also usable outside collect_status)."""
    clock = now or utc_now()
    is_act = is_active(meta, clone) if active is None else active
    progress = _parse_progress(clone)
    phase = _phase_from_lifecycle(meta)
    terminal = _is_terminal_inactive(meta)
    elapsed = _elapsed_for_meta(meta, clock)
    timeout, remaining = _timeout_and_remaining(
        meta, progress, elapsed, terminal=terminal
    )
    activity_at, activity_source = _last_activity_at(
        meta,
        progress,
        clone,
        now=clock,
        include_workspace=not terminal,
    )
    from grok_worker.activity_lease import lease_summary

    lease = lease_summary(clone, now=clock)
    timeout_mode = "fixed_legacy"
    hard_timeout: int | None = None
    hard_remaining: float | None = None
    lease_revision: int | None = None
    if lease is not None:
        timeout_mode = "activity_lease"
        raw_idle = lease["idle_timeout_seconds"]
        if isinstance(raw_idle, int) and not isinstance(raw_idle, bool):
            timeout = raw_idle
        raw_remaining = lease["lease_remaining_seconds"]
        remaining = (
            None
            if terminal or not isinstance(raw_remaining, (int, float))
            else float(raw_remaining)
        )
        raw_hard = lease["hard_timeout_seconds"]
        hard_timeout = raw_hard if isinstance(raw_hard, int) else None
        raw_hard_remaining = lease["hard_remaining_seconds"]
        hard_remaining = (
            None
            if terminal or not isinstance(raw_hard_remaining, (int, float))
            else float(raw_hard_remaining)
        )
        raw_revision = lease["lease_revision"]
        lease_revision = raw_revision if isinstance(raw_revision, int) else None
        lease_activity = _parse_iso_utc(lease["lease_last_activity_at"])
        current_activity = _parse_iso_utc(activity_at)
        if lease_activity is not None and (
            current_activity is None or lease_activity > current_activity
        ):
            activity_at = lease_activity.isoformat()
            activity_source = str(lease["lease_activity_source"])
    return {
        "phase": phase,
        "last_activity_at": activity_at,
        "activity_source": activity_source,
        "progress_step": _progress_step(progress),
        "elapsed_seconds": elapsed,
        "timeout_seconds": timeout,
        "remaining_seconds": remaining,
        "timeout_mode": timeout_mode,
        "hard_timeout_seconds": hard_timeout,
        "hard_remaining_seconds": hard_remaining,
        "lease_revision": lease_revision,
        "result_ready": _result_ready(clone),
        "artifact_ready": _artifact_ready(meta),
        "resources": _resources_for(meta, is_act),
    }


def collect_status(
    disposable_root: Path,
    *,
    cap_bytes: int = DEFAULT_CAP_BYTES,
    shared_cache_root: Path | None = None,
) -> StatusReport:
    root = disposable_root
    shared = (shared_cache_root or default_shared_cache_root()).resolve()
    usage = root_usage_bytes(root) if root.is_dir() else 0
    clones: list[CloneStatus] = []
    unmarked: list[str] = []
    reclaimable = 0
    now = utc_now()

    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
                continue
            size = dir_size_bytes(child)
            if not is_managed_clone(child):
                unmarked.append(child.name)
                continue
            try:
                meta = WorkerMeta.read(meta_path(child))
            except (OSError, ValueError, KeyError):
                unmarked.append(child.name)
                continue
            if not meta_is_trusted(meta):
                unmarked.append(child.name)
                continue
            active = is_active(meta, child)
            reclaim = (not active) and should_delete(meta, child, now, disposable_root=root)
            if reclaim:
                reclaimable += size
            summary = build_clone_summary(meta, child, now=now, active=active)
            resources_raw = summary["resources"]
            if isinstance(resources_raw, dict):
                resources_val: dict[str, float | int | None] = {
                    str(k): (
                        v
                        if isinstance(v, (int, float)) and not isinstance(v, bool)
                        else None
                    )
                    for k, v in resources_raw.items()
                }
            else:
                resources_val = empty_resources()
            elapsed_val = float(summary["elapsed_seconds"])
            timeout_val = summary["timeout_seconds"]
            remaining_val = summary["remaining_seconds"]
            clones.append(
                CloneStatus(
                    name=child.name,
                    state=str(meta.state),
                    size_bytes=size,
                    reclaimable=reclaim,
                    keep_reason=meta.keep_reason,
                    retention_deadline=meta.retention_deadline,
                    pid=meta.runner_pid or meta.pid,
                    active=active,
                    phase=str(summary["phase"]),
                    last_activity_at=str(summary["last_activity_at"]),
                    activity_source=str(summary["activity_source"]),
                    progress_step=(
                        str(summary["progress_step"])
                        if summary["progress_step"] is not None
                        else None
                    ),
                    elapsed_seconds=elapsed_val,
                    timeout_seconds=(
                        timeout_val
                        if isinstance(timeout_val, (int, float))
                        and not isinstance(timeout_val, bool)
                        else None
                    ),
                    remaining_seconds=(
                        remaining_val
                        if isinstance(remaining_val, (int, float))
                        and not isinstance(remaining_val, bool)
                        else None
                    ),
                    timeout_mode=str(summary["timeout_mode"]),
                    hard_timeout_seconds=(
                        summary["hard_timeout_seconds"]
                        if isinstance(summary["hard_timeout_seconds"], int)
                        and not isinstance(summary["hard_timeout_seconds"], bool)
                        else None
                    ),
                    hard_remaining_seconds=(
                        float(summary["hard_remaining_seconds"])
                        if isinstance(summary["hard_remaining_seconds"], (int, float))
                        and not isinstance(summary["hard_remaining_seconds"], bool)
                        else None
                    ),
                    lease_revision=(
                        summary["lease_revision"]
                        if isinstance(summary["lease_revision"], int)
                        and not isinstance(summary["lease_revision"], bool)
                        else None
                    ),
                    result_ready=bool(summary["result_ready"]),
                    artifact_ready=bool(summary["artifact_ready"]),
                    resources=resources_val,
                )
            )

    return StatusReport(
        disposable_root=str(root.resolve()) if root.exists() else str(root),
        usage_bytes=usage,
        cap_bytes=cap_bytes,
        over_cap=usage > cap_bytes,
        reclaimable_bytes=reclaimable,
        shared_cache_root=str(shared),
        shared_uv_cache=str(shared / "uv"),
        shared_venvs=str(shared / "venvs"),
        shared_locks=str(shared / "locks"),
        clones=clones,
        unmarked_legacy=unmarked,
    )


def format_status_text(report: StatusReport) -> str:
    lines = [
        f"disposable_root: {report.disposable_root}",
        f"usage_bytes: {report.usage_bytes}",
        f"cap_bytes: {report.cap_bytes}",
        f"over_cap: {report.over_cap}",
        f"reclaimable_bytes: {report.reclaimable_bytes}",
        f"shared_cache_root: {report.shared_cache_root}",
        f"shared_uv_cache: {report.shared_uv_cache}",
        f"shared_venvs: {report.shared_venvs}",
        f"shared_locks: {report.shared_locks}",
        f"managed_clones: {len(report.clones)}",
        f"unmarked_legacy: {len(report.unmarked_legacy)}",
    ]
    for c in report.clones:
        lines.append(
            f"  - {c.name}: state={c.state} phase={c.phase} size={c.size_bytes} "
            f"reclaimable={c.reclaimable} active={c.active} "
            f"elapsed={c.elapsed_seconds:.0f}s result_ready={c.result_ready}"
        )
    for name in report.unmarked_legacy:
        lines.append(f"  - {name}: UNMARKED_LEGACY")
    return "\n".join(lines) + "\n"


def format_status_json(report: StatusReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
