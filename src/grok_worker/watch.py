"""Low-noise event-first worker observation with a health heartbeat fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from grok_worker.completion_events import list_completion_events
from grok_worker.health import collect_health

_ACTIVE_STATES = frozenset({"creating", "running", "finalizing", "session_open"})
_ATTENTION_STATES = frozenset({"failed", "startup_failed", "worker_crashed"})
_HEALTH_KEYS = (
    "task_id",
    "run_id",
    "dispatcher_id",
    "state",
    "phase",
    "last_activity_at",
    "activity_source",
    "progress_step",
    "elapsed_seconds",
    "remaining_seconds",
    "hard_remaining_seconds",
    "result_ready",
    "artifact_ready",
    "active",
    "runner_live",
    "process_live",
    "backend",
    "resources",
)


def _event_needs_attention(event: dict[str, Any]) -> bool:
    return bool(
        event.get("attention_required") is True
        or event.get("kind") == "attention"
        or event.get("state") in _ATTENTION_STATES
    )


def _compact_health(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in _HEALTH_KEYS if key in row}


def _health_needs_attention(row: dict[str, Any]) -> bool:
    state = str(row.get("state") or "")
    if state in _ATTENTION_STATES:
        return True
    if state not in _ACTIVE_STATES:
        return False
    if row.get("active") is not True or row.get("runner_live") is not True:
        return True
    process_pid = row.get("process_pid")
    return process_pid is not None and row.get("process_live") is not True


def watch_workers(
    *,
    shared_cache_root: Path,
    disposable_root: Path,
    after: str = "",
    wait_seconds: float,
    run_id: str | None = None,
    dispatcher_id: str | None = None,
) -> dict[str, Any]:
    """Wait for an event, otherwise return one compact read-only health snapshot."""
    if not run_id and not dispatcher_id:
        raise ValueError("watch requires --run-id or --dispatcher-id")

    events = list_completion_events(
        shared_cache_root=shared_cache_root,
        after=after,
        wait_seconds=wait_seconds,
        run_id=run_id,
        dispatcher_id=dispatcher_id,
    )
    if events:
        return {
            "kind": "events",
            "events": events,
            "count": len(events),
            "next_cursor": str(events[-1].get("event_id") or after),
            "attention_required": any(_event_needs_attention(item) for item in events),
        }

    report = collect_health(disposable_root, dispatcher_id=dispatcher_id)
    rows = report.clones
    if run_id:
        rows = [row for row in rows if row.get("run_id") == run_id]
    compact = [_compact_health(row) for row in rows]
    attention = any(_health_needs_attention(row) for row in rows)
    reason_code: str | None = None
    if run_id and not rows:
        attention = True
        reason_code = "run_not_found_after_wait"
    elif attention:
        reason_code = "health_attention_required"

    return {
        "kind": "heartbeat",
        "events": [],
        "count": 0,
        "next_cursor": after,
        "attention_required": attention,
        "reason_code": reason_code,
        "workers": compact,
        "health_interval_seconds": report.interval_seconds,
    }


__all__ = ["watch_workers"]
