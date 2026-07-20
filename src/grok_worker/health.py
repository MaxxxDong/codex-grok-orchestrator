"""Diagnostic-only health inspection (read-only; never terminates workers).

Health inspection interval is HEALTH_INSPECT_INTERVAL_SECONDS (300). Inspecting
lifecycle, last activity, PID/process identity, CPU/RSS/status records health
but does not terminate, interrupt, restart, or mutate a running worker merely
because a health interval elapsed. Actual termination remains governed by the
the worker-owned activity lease or a separate positively proven dead/stale process
reconciliation path (GC).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from grok_worker.constants import HEALTH_INSPECT_INTERVAL_SECONDS
from grok_worker.gc import is_active
from grok_worker.models import WorkerMeta, meta_is_trusted
from grok_worker.paths import is_managed_clone, meta_path
from grok_worker.process_identity import process_matches
from grok_worker.resources import empty_resources, process_resources
from grok_worker.status import build_clone_summary, preferred_resource_pid


@dataclass
class HealthReport:
    """Pointer-safe health snapshot for one clone or disposable root."""

    interval_seconds: int = HEALTH_INSPECT_INTERVAL_SECONDS
    diagnostic_only: bool = True
    mutates_worker: bool = False
    roots: list[str] = field(default_factory=list)
    clones: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_clone_health(meta: WorkerMeta, clone: Path) -> dict[str, Any]:
    """Read-only health fields; never writes lifecycle or signals processes."""
    active = is_active(meta, clone)
    summary = build_clone_summary(meta, clone, active=active)
    pid = preferred_resource_pid(meta)
    resources = process_resources(pid) if active else empty_resources()
    runner_live = process_matches(meta.runner_pid, meta.runner_start_token)
    acpx_live = process_matches(meta.acpx_pid, meta.acpx_start_token)
    return {
        "task_id": meta.task_id,
        "run_id": meta.run_id,
        "dispatcher_id": meta.dispatcher_id,
        "state": str(meta.state),
        "phase": summary.get("phase"),
        "last_activity_at": summary.get("last_activity_at"),
        "activity_source": summary.get("activity_source"),
        "progress_step": summary.get("progress_step"),
        "elapsed_seconds": summary.get("elapsed_seconds"),
        "timeout_seconds": summary.get("timeout_seconds"),
        "remaining_seconds": summary.get("remaining_seconds"),
        "timeout_mode": summary.get("timeout_mode"),
        "hard_timeout_seconds": summary.get("hard_timeout_seconds"),
        "hard_remaining_seconds": summary.get("hard_remaining_seconds"),
        "lease_revision": summary.get("lease_revision"),
        "result_ready": summary.get("result_ready"),
        "artifact_ready": summary.get("artifact_ready"),
        "terminal_event_ready": meta.terminal_event_ready,
        "active": active,
        "runner_pid": meta.runner_pid,
        "runner_live": runner_live,
        "backend": meta.backend,
        "process_pid": meta.acpx_pid,
        "process_live": acpx_live,
        # Compatibility aliases retained for v0.3/v0.4 consumers.
        "acpx_pid": meta.acpx_pid,
        "acpx_live": acpx_live,
        "resources": resources,
        "health_interval_seconds": HEALTH_INSPECT_INTERVAL_SECONDS,
        "diagnostic_only": True,
    }


def collect_health(
    disposable_root: Path,
    *,
    dispatcher_id: str | None = None,
) -> HealthReport:
    """Inspect all managed clones under *disposable_root* without mutation."""
    report = HealthReport()
    root = Path(disposable_root)
    report.roots.append(str(root.resolve()))
    if not root.is_dir():
        return report
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
            continue
        if not is_managed_clone(child):
            continue
        try:
            meta = WorkerMeta.read(meta_path(child))
        except (OSError, ValueError, KeyError):
            continue
        if not meta_is_trusted(meta):
            continue
        if dispatcher_id is not None and meta.dispatcher_id != dispatcher_id:
            continue
        row = inspect_clone_health(meta, child)
        row["disposable_root"] = str(root.resolve())
        report.clones.append(row)
    return report
