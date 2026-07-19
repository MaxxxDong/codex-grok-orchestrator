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

from grok_worker.constants import DEFAULT_EVENT_WAIT_SECONDS, MAX_EVENT_WAIT_SECONDS
from grok_worker.locks import FileLock
from grok_worker.models import dt_to_iso, utc_now
from grok_worker.paths import default_shared_cache_root

# Allowed event keys only — never prompt, tokens, env, or agent output.
_ALLOWED_EVENT_KEYS = frozenset(
    {
        "event_id",
        "task_id",
        "state",
        "timestamp",
        "artifact_path",
        "run_id",
        "dispatcher_id",
        "kind",
        "exit_code",
        "artifact_ready",
        "clone_cleaned",
        "session_cleaned",
        "attention_required",
        "reason_code",
    }
)

# Required pointer fields with accepted types (artifact_path may be null).
# Extended fields are optional for backward-compatible reads of old rows.
_REQUIRED_STRING_KEYS = ("event_id", "task_id", "state", "timestamp")
_OPTIONAL_STRING_KEYS = ("run_id", "dispatcher_id", "kind", "reason_code")
_OPTIONAL_BOOL_KEYS = (
    "artifact_ready",
    "clone_cleaned",
    "session_cleaned",
    "attention_required",
)

NOTIFICATIONS_DIR = "notifications"
COMPLETION_EVENTS_LOG = "completion-events.jsonl"
COMPLETION_EVENTS_LOCK = "completion-events.lock"

# Exceptions that belong only to the notification I/O / serialization boundary.
# Callers of the best-effort emit path must not let these reverse lifecycle work.
_NOTIFICATION_IO_ERRORS = (OSError, TypeError, ValueError, json.JSONDecodeError)


class EventWaitError(ValueError):
    """Invalid wait_seconds (negative or greater than MAX_EVENT_WAIT_SECONDS)."""


def completion_events_path(shared_cache_root: Path) -> Path:
    return Path(shared_cache_root) / NOTIFICATIONS_DIR / COMPLETION_EVENTS_LOG


def completion_events_lock_path(shared_cache_root: Path) -> Path:
    return Path(shared_cache_root) / NOTIFICATIONS_DIR / COMPLETION_EVENTS_LOCK


def _resolve_shared(shared_cache_root: Path | None) -> Path:
    if shared_cache_root is not None:
        return Path(shared_cache_root).resolve()
    return default_shared_cache_root().resolve()


def validate_wait_seconds(wait_seconds: float) -> float:
    """Accept 0 (nonblocking) through MAX_EVENT_WAIT_SECONDS; reject negatives / over max."""
    try:
        value = float(wait_seconds)
    except (TypeError, ValueError) as exc:
        raise EventWaitError("wait_seconds must be a number") from exc
    if value < 0:
        raise EventWaitError("wait_seconds must not be negative")
    if value > MAX_EVENT_WAIT_SECONDS:
        raise EventWaitError(
            f"wait_seconds must be <= {MAX_EVENT_WAIT_SECONDS} "
            f"(got {value}); callers may repeat waits"
        )
    return value


def _validate_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Return a sanitized event, or None if the row is malformed / incomplete.

    Incomplete objects such as ``{}`` must never surface as events.
    Old rows without run_id/dispatcher_id remain valid for unfiltered reads.
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
    for opt in _OPTIONAL_STRING_KEYS:
        if opt in out and out[opt] is not None and not isinstance(out[opt], str):
            return None
    for opt in _OPTIONAL_BOOL_KEYS:
        if opt in out and not isinstance(out[opt], bool):
            return None
    if "exit_code" in out:
        exit_code = out["exit_code"]
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
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


def _event_kind(event: dict[str, Any]) -> str:
    """Treat pre-kind rows as terminal notifications."""
    value = event.get("kind")
    return value if isinstance(value, str) and value else "terminal"


def _has_run_state_kind(
    events: list[dict[str, Any]], run_id: str, state: str, kind: str
) -> bool:
    for event in events:
        if (
            event.get("run_id") == run_id
            and event.get("state") == state
            and _event_kind(event) == kind
        ):
            return True
    return False


def _has_task_state_kind_legacy(
    events: list[dict[str, Any]], task_id: str, state: str, kind: str
) -> bool:
    """Legacy dedup when run_id is absent: (task_id, state, kind)."""
    for event in events:
        if event.get("run_id"):
            continue
        if (
            event.get("task_id") == task_id
            and event.get("state") == state
            and _event_kind(event) == kind
        ):
            return True
    return False


def emit_completion_event(
    *,
    task_id: str,
    state: str,
    artifact_path: str | None = None,
    shared_cache_root: Path | None = None,
    timestamp: str | None = None,
    run_id: str | None = None,
    dispatcher_id: str | None = None,
    kind: str = "terminal",
    exit_code: int | None = None,
    artifact_ready: bool | None = None,
    clone_cleaned: bool | None = None,
    session_cleaned: bool | None = None,
    attention_required: bool | None = None,
    reason_code: str | None = None,
) -> dict[str, Any] | None:
    """Append one deduplicated pointer notification (best-effort).

    Dedup key is (run_id, state, kind) when run_id is present; otherwise legacy
    (task_id, state, kind) among events without run_id. Concurrent appends take an
    exclusive lock so no half-line JSON is written. Returns the event dict when
    appended, or None when the same terminal notification already exists,
    arguments are empty, or a notification-boundary I/O/serialization error occurs.

    Failures at this seam must not raise into finalize/GC callers that already
    persisted authoritative lifecycle state. ``kind`` is terminal, settled, or
    attention for current writers; readers preserve unknown future kinds.
    """
    if not task_id or not state or not kind:
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
            "kind": str(kind),
        }
        if run_id:
            event["run_id"] = str(run_id)
        if dispatcher_id:
            event["dispatcher_id"] = str(dispatcher_id)
        if exit_code is not None:
            event["exit_code"] = exit_code
        if artifact_ready is not None:
            event["artifact_ready"] = artifact_ready
        if clone_cleaned is not None:
            event["clone_cleaned"] = clone_cleaned
        if session_cleaned is not None:
            event["session_cleaned"] = session_cleaned
        if attention_required is not None:
            event["attention_required"] = attention_required
        if reason_code:
            event["reason_code"] = str(reason_code)
        validated = _validate_event(event)
        if validated is None:
            return None
        event = validated
        with lock:
            existing = _read_events_unlocked(log_path)
            if run_id:
                if _has_run_state_kind(existing, str(run_id), str(state), str(kind)):
                    return None
            elif _has_task_state_kind_legacy(
                existing, str(task_id), str(state), str(kind)
            ):
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
    run_id: str | None = None,
    dispatcher_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return events strictly after *after* (empty = from start).

    Optional *run_id* / *dispatcher_id* filters keep unfiltered reads compatible.
    When *wait_seconds* > 0 and no matching events exist yet, poll until one
    appears or the wait budget is exhausted. Malformed JSONL rows are skipped.
    Raises EventWaitError for invalid wait bounds.
    """
    wait = validate_wait_seconds(wait_seconds)
    shared = _resolve_shared(shared_cache_root)
    log_path = completion_events_path(shared)
    deadline = time.monotonic() + max(0.0, float(wait))

    def _slice() -> list[dict[str, Any]]:
        rows = _read_events_unlocked(log_path)
        if after:
            idx = None
            for i, row in enumerate(rows):
                if row.get("event_id") == after:
                    idx = i
                    break
            if idx is None:
                # Unknown cursor: return none until the cursor exists, then tail.
                rows = []
            else:
                rows = rows[idx + 1 :]
        if run_id:
            rows = [r for r in rows if r.get("run_id") == run_id]
        if dispatcher_id:
            rows = [r for r in rows if r.get("dispatcher_id") == dispatcher_id]
        return rows

    while True:
        matched = _slice()
        if matched:
            return matched
        if wait <= 0 or time.monotonic() >= deadline:
            return matched
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return matched
        time.sleep(min(poll_interval, remaining))


def events_to_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"events": events, "count": len(events)}


# Re-export defaults for CLI/docs.
__all__ = [
    "DEFAULT_EVENT_WAIT_SECONDS",
    "MAX_EVENT_WAIT_SECONDS",
    "EventWaitError",
    "completion_events_path",
    "completion_events_lock_path",
    "emit_completion_event",
    "events_to_payload",
    "list_completion_events",
    "validate_wait_seconds",
]
