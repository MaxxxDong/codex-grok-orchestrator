"""Shared-cache completion-event notification index (not a second state source).

Lifecycle metadata remains authoritative. Events are non-sensitive pointers only
for waiters/pollers under the shared cache root.

Notification writes are best-effort: I/O or serialization failures at this
boundary must not reverse an already-persisted lifecycle transition, RunOutcome,
artifact path, or GC reconcile result.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from grok_worker.locks import FileLock
from grok_worker.models import dt_to_iso, utc_now
from grok_worker.paths import default_shared_cache_root

# Allowed event keys only — never prompt, tokens, env, or agent output.
_ALLOWED_EVENT_KEYS = frozenset(
    {"event_id", "task_id", "state", "timestamp", "artifact_path"}
)

# Required pointer fields with accepted types (artifact_path may be null).
_REQUIRED_STRING_KEYS = ("event_id", "task_id", "state", "timestamp")

NOTIFICATIONS_DIR = "notifications"
COMPLETION_EVENTS_LOG = "completion-events.jsonl"
COMPLETION_EVENTS_LOCK = "completion-events.lock"

# Exceptions that belong only to the notification I/O / serialization boundary.
# Callers of the best-effort emit path must not let these reverse lifecycle work.
_NOTIFICATION_IO_ERRORS = (OSError, TypeError, ValueError, json.JSONDecodeError)


def completion_events_path(shared_cache_root: Path) -> Path:
    return Path(shared_cache_root) / NOTIFICATIONS_DIR / COMPLETION_EVENTS_LOG


def completion_events_lock_path(shared_cache_root: Path) -> Path:
    return Path(shared_cache_root) / NOTIFICATIONS_DIR / COMPLETION_EVENTS_LOCK


def _resolve_shared(shared_cache_root: Path | None) -> Path:
    if shared_cache_root is not None:
        return Path(shared_cache_root).resolve()
    return default_shared_cache_root().resolve()


def _validate_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Return a sanitized event, or None if the row is malformed / incomplete.

    Incomplete objects such as ``{}`` must never surface as events.
    """
    out: dict[str, Any] = {}
    for key in _ALLOWED_EVENT_KEYS:
        if key not in raw:
            continue
        out[key] = raw[key]
    for key in _REQUIRED_STRING_KEYS:
        value = out.get(key)
        if not isinstance(value, str) or not value:
            return None
    if "artifact_path" not in out:
        return None
    art = out["artifact_path"]
    if art is not None and not isinstance(art, str):
        return None
    return out


def _read_events_unlocked(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.is_file() or log_path.is_symlink():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event = _validate_event({str(k): v for k, v in payload.items()})
        if event is not None:
            rows.append(event)
    return rows


def _has_task_state(events: list[dict[str, Any]], task_id: str, state: str) -> bool:
    for event in events:
        if event.get("task_id") == task_id and event.get("state") == state:
            return True
    return False


def emit_completion_event(
    *,
    task_id: str,
    state: str,
    artifact_path: str | None = None,
    shared_cache_root: Path | None = None,
    timestamp: str | None = None,
) -> dict[str, Any] | None:
    """Append one deduplicated terminal-state notification (best-effort).

    Dedup key is (task_id, state). Concurrent appends take an exclusive lock so
    no half-line JSON is written. Returns the event dict when appended, or None
    when the same terminal notification already exists, arguments are empty, or
    a notification-boundary I/O/serialization error occurs.

    Failures at this seam must not raise into finalize/GC callers that already
    persisted authoritative lifecycle state.
    """
    if not task_id or not state:
        return None
    try:
        shared = _resolve_shared(shared_cache_root)
        log_path = completion_events_path(shared)
        lock = FileLock(completion_events_lock_path(shared))
        ts = timestamp or (dt_to_iso(utc_now()) or "")
        event: dict[str, Any] = {
            "event_id": str(uuid.uuid4()),
            "task_id": str(task_id),
            "state": str(state),
            "timestamp": ts,
            "artifact_path": artifact_path,
        }
        validated = _validate_event(event)
        if validated is None:
            return None
        event = validated
        with lock:
            existing = _read_events_unlocked(log_path)
            if _has_task_state(existing, str(task_id), str(state)):
                return None
            log_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                try:
                    import os

                    os.fsync(fh.fileno())
                except OSError:
                    # fsync is best-effort; the line is already flushed.
                    pass
        return event
    except _NOTIFICATION_IO_ERRORS:
        # Advisory only: never reverse authoritative lifecycle / GC work.
        return None


def list_completion_events(
    *,
    shared_cache_root: Path | None = None,
    after: str = "",
    wait_seconds: float = 0.0,
    poll_interval: float = 0.05,
) -> list[dict[str, Any]]:
    """Return events strictly after *after* (empty = from start).

    When *wait_seconds* > 0 and no matching events exist yet, poll until one
    appears or the wait budget is exhausted. Malformed JSONL rows are skipped.
    """
    shared = _resolve_shared(shared_cache_root)
    log_path = completion_events_path(shared)
    deadline = time.monotonic() + max(0.0, float(wait_seconds))

    def _slice() -> list[dict[str, Any]]:
        rows = _read_events_unlocked(log_path)
        if not after:
            return rows
        idx = None
        for i, row in enumerate(rows):
            if row.get("event_id") == after:
                idx = i
                break
        if idx is None:
            # Unknown cursor: return none until the cursor exists, then tail.
            return []
        return rows[idx + 1 :]

    while True:
        matched = _slice()
        if matched:
            return matched
        if wait_seconds <= 0 or time.monotonic() >= deadline:
            return matched
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return matched
        time.sleep(min(poll_interval, remaining))


def events_to_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"events": events, "count": len(events)}
