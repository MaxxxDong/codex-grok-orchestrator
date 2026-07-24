"""Low-noise event-first worker observation with a health heartbeat fallback."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from grok_worker.completion_events import list_completion_events
from grok_worker.health import collect_health

_ACTIVE_STATES = frozenset({"creating", "running", "finalizing", "session_open"})
_ATTENTION_STATES = frozenset({"failed", "startup_failed", "worker_crashed"})
_HEALTH_FALLBACK_SECONDS = 1.0
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
    "terminal_event_ready",
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


def _with_delivery_latency(event: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(event)
    timestamp = event.get("emitted_at", event.get("timestamp"))
    if not isinstance(timestamp, str):
        return enriched
    try:
        emitted = datetime.fromisoformat(timestamp)
    except ValueError:
        return enriched
    if emitted.tzinfo is None:
        emitted = emitted.replace(tzinfo=UTC)
    latency = (datetime.now(UTC) - emitted.astimezone(UTC)).total_seconds()
    enriched["watch_delivery_latency_seconds"] = round(max(0.0, latency), 6)
    return enriched


def watch_workers(
    *,
    shared_cache_root: Path,
    disposable_root: Path,
    after: str = "",
    wait_seconds: float,
    run_id: str | None = None,
    dispatcher_id: str | None = None,
    until_settled: bool = False,
) -> dict[str, Any]:
    """Wait for events, optionally carrying one run through cleanup settlement."""
    if not run_id and not dispatcher_id:
        raise ValueError("watch requires --run-id or --dispatcher-id")
    if until_settled and not run_id:
        raise ValueError("--until-settled requires --run-id")

    cursor = after
    deadline = monotonic() + max(0.0, wait_seconds)
    delivered: list[dict[str, Any]] = []
    while True:
        remaining = max(0.0, deadline - monotonic())
        event_wait = min(remaining, _HEALTH_FALLBACK_SECONDS) if run_id else remaining
        events = list_completion_events(
            shared_cache_root=shared_cache_root,
            after=cursor,
            wait_seconds=event_wait,
            run_id=run_id,
            dispatcher_id=dispatcher_id,
        )
        if not events:
            if run_id and remaining > event_wait:
                report = collect_health(disposable_root, dispatcher_id=dispatcher_id)
                rows = [row for row in report.clones if row.get("run_id") == run_id]
                terminal = [
                    row
                    for row in rows
                    if str(row.get("state") or "") not in _ACTIVE_STATES
                ]
                if terminal and not delivered:
                    compact = [_compact_health(row) for row in terminal]
                    return {
                        "kind": "heartbeat",
                        "events": [],
                        "count": 0,
                        "next_cursor": cursor,
                        "attention_required": True,
                        "reason_code": "terminal_notification_missing",
                        "workers": compact,
                        "health_interval_seconds": report.interval_seconds,
                    }
                if delivered and not rows:
                    return {
                        "kind": "events",
                        "events": delivered,
                        "count": len(delivered),
                        "next_cursor": cursor,
                        "attention_required": True,
                        "reason_code": "settled_notification_missing_after_cleanup",
                        "settled": False,
                    }
                continue
            break
        batch = [_with_delivery_latency(item) for item in events]
        delivered.extend(batch)
        cursor = str(batch[-1].get("event_id") or cursor)
        settled = any(item.get("kind") == "settled" for item in batch)
        live_attention = any(item.get("kind") == "attention" for item in batch)
        if not until_settled or settled or live_attention or monotonic() >= deadline:
            break

    if delivered:
        return {
            "kind": "events",
            "events": delivered,
            "count": len(delivered),
            "next_cursor": cursor,
            "attention_required": any(_event_needs_attention(item) for item in delivered),
            "settled": any(item.get("kind") == "settled" for item in delivered),
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
        "next_cursor": cursor,
        "attention_required": attention,
        "reason_code": reason_code,
        "workers": compact,
        "health_interval_seconds": report.interval_seconds,
    }


__all__ = ["watch_workers"]
