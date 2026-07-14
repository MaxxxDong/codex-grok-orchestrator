"""Completion-event notification seam (shared cache JSONL + events CLI).

Public contract:
- Terminal-state transitions append one deduplicated JSON event under the shared
  cache notification log.
- ``grok-worker events --after <event_id> --wait-seconds <n> --json`` can query
  or boundedly wait for new events.
- Events carry only non-sensitive pointers (task_id/state/artifact_path/
  timestamp/event_id); lifecycle files remain the source of truth.
- Repeated finalize/reconcile must not emit duplicate notifications.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grok_worker.cli import main
from grok_worker.constants import MANAGED_BY, SCHEMA_VERSION
from grok_worker.gc import convert_dead_worker
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import meta_dir, meta_path
from grok_worker.runner import RunConfig, run_worker

SENSITIVE_KEYS = frozenset(
    {
        "prompt",
        "api_key",
        "apiKey",
        "authorization",
        "token",
        "secret",
        "password",
        "env",
        "environment",
        "mcp_config",
        "agent_output",
        "stdout",
        "stderr",
    }
)
REQUIRED_EVENT_KEYS = frozenset(
    {"event_id", "task_id", "state", "timestamp", "artifact_path"}
)


def _notification_log(shared: Path) -> Path:
    """Expected notification log location under the shared cache root."""
    return shared / "notifications" / "completion-events.jsonl"


def _write_dead_running_clone(clone: Path, task_id: str) -> WorkerMeta:
    now = utc_now()
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id=task_id,
        source_realpath="/tmp/src-for-events-red",
        clone_realpath=str(clone.resolve()),
        state=WorkerState.RUNNING,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        runner_pid=999_999_991,
        runner_start_token="not-a-real-token",
        pid=999_999_991,
    )
    meta_dir(clone).mkdir(parents=True, exist_ok=True)
    meta.write(meta_path(clone))
    return meta


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        assert isinstance(row, dict), "each notification line must be a JSON object"
        rows.append(row)
    return rows


def _assert_event_shape(event: dict) -> None:
    missing = REQUIRED_EVENT_KEYS - set(event)
    assert not missing, f"event missing required keys: {sorted(missing)}"
    for key in SENSITIVE_KEYS:
        assert key not in event, f"event must not include sensitive key {key!r}"
    assert isinstance(event["event_id"], str) and event["event_id"]
    assert isinstance(event["task_id"], str) and event["task_id"]
    assert isinstance(event["state"], str) and event["state"]
    assert isinstance(event["timestamp"], str) and event["timestamp"]
    # artifact_path may be null for failures without artifacts
    assert "artifact_path" in event
    if event["artifact_path"] is not None:
        assert isinstance(event["artifact_path"], str)


def test_terminal_finalize_emits_deduped_completion_event(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy path: successful finalize appends exactly one queryable event."""
    shared = tmp_roots["shared"]
    monkeypatch.setenv("GROK_WORKER_CACHE_ROOT", str(shared))
    cfg = RunConfig(
        source=git_source,
        prompt="emit completion event",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=shared,
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        task_id="evt-ok-01",
        skip_post_gc=True,
    )
    outcome = run_worker(cfg)
    assert outcome.exit_code == 0
    assert outcome.state == "success"

    log_path = _notification_log(shared)
    assert log_path.is_file(), (
        "terminal finalize must append a JSONL notification under "
        f"{log_path} (shared cache notifications log)"
    )
    events = _load_jsonl(log_path)
    matching = [e for e in events if e.get("task_id") == "evt-ok-01"]
    assert len(matching) == 1, (
        f"expected exactly one completion event for task, got {len(matching)}: {matching}"
    )
    event = matching[0]
    _assert_event_shape(event)
    assert event["state"] == "success"
    if outcome.artifact_path:
        assert event["artifact_path"] == outcome.artifact_path

    # CLI query seam: events after empty cursor returns the new event as JSON.
    code = main(
        [
            "events",
            "--shared-cache-root",
            str(shared),
            "--after",
            "",
            "--wait-seconds",
            "0",
            "--json",
        ]
    )
    assert code == 0, "events CLI must exist and return 0 for a non-blocking poll"
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, (dict, list))
    if isinstance(payload, dict):
        listed = payload.get("events", payload.get("items", []))
    else:
        listed = payload
    assert any(
        isinstance(item, dict) and item.get("task_id") == "evt-ok-01" for item in listed
    ), f"events CLI JSON must include the completion event; got {payload!r}"


def test_duplicate_reconcile_does_not_renotify(
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary: converting the same dead worker twice must not duplicate events."""
    shared = tmp_roots["shared"]
    monkeypatch.setenv("GROK_WORKER_CACHE_ROOT", str(shared))
    clone = tmp_roots["disposable"] / "evt-dead-01"
    clone.mkdir()
    (clone / "marker.txt").write_text("x\n", encoding="utf-8")
    meta = _write_dead_running_clone(clone, "evt-dead-01")

    first = convert_dead_worker(meta, clone, reason="worker process died")
    assert first.state == WorkerState.FAILED

    meta2 = WorkerMeta.read(meta_path(clone))
    second = convert_dead_worker(meta2, clone, reason="worker process died")
    assert second.state == WorkerState.FAILED

    log_path = _notification_log(shared)
    assert log_path.is_file(), (
        "reconcile terminal transition must write shared-cache notification log "
        f"at {log_path}"
    )
    events = _load_jsonl(log_path)
    matching = [e for e in events if e.get("task_id") == "evt-dead-01"]
    assert len(matching) == 1, (
        "duplicate finalize/reconcile must not re-notify; "
        f"expected 1 event, got {len(matching)}: {matching}"
    )
    _assert_event_shape(matching[0])
    assert matching[0]["state"] == "failed"


def test_events_after_cursor_and_wait_boundary(
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cursor: --after skips past event_id; wait-seconds 0 returns immediately."""
    from grok_worker.completion_events import emit_completion_event, list_completion_events

    shared = tmp_roots["shared"]
    monkeypatch.setenv("GROK_WORKER_CACHE_ROOT", str(shared))
    first = emit_completion_event(
        task_id="cursor-a",
        state="success",
        artifact_path="/tmp/art-a",
        shared_cache_root=shared,
    )
    second = emit_completion_event(
        task_id="cursor-b",
        state="failed",
        artifact_path=None,
        shared_cache_root=shared,
    )
    assert first is not None and second is not None

    after_first = list_completion_events(
        shared_cache_root=shared, after=str(first["event_id"]), wait_seconds=0
    )
    assert len(after_first) == 1
    assert after_first[0]["task_id"] == "cursor-b"

    after_second = list_completion_events(
        shared_cache_root=shared, after=str(second["event_id"]), wait_seconds=0
    )
    assert after_second == []

    code = main(
        [
            "events",
            "--shared-cache-root",
            str(shared),
            "--after",
            str(first["event_id"]),
            "--wait-seconds",
            "0",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    listed = payload.get("events", payload) if isinstance(payload, dict) else payload
    assert any(isinstance(i, dict) and i.get("task_id") == "cursor-b" for i in listed)


class _BoomNotificationLock:
    """FileLock stand-in that fails at the notification I/O boundary."""

    def __init__(self, path: object, shared: bool = False) -> None:
        self.path = path
        self.shared = shared

    def __enter__(self) -> _BoomNotificationLock:
        raise OSError("simulated notification lock/fsync failure")

    def __exit__(self, *args: object) -> None:
        return None

    def acquire(self) -> None:
        raise OSError("simulated notification lock/fsync failure")

    def release(self) -> None:
        return None


def test_finalize_succeeds_when_notification_io_fails(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Advisory boundary: notification OSError must not reverse successful finalize."""
    from grok_worker import completion_events as ce

    shared = tmp_roots["shared"]
    monkeypatch.setenv("GROK_WORKER_CACHE_ROOT", str(shared))
    monkeypatch.setattr(ce, "FileLock", _BoomNotificationLock)

    cfg = RunConfig(
        source=git_source,
        prompt="notify fail soft",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=shared,
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        task_id="evt-notify-io-fail",
        skip_post_gc=True,
    )
    outcome = run_worker(cfg)
    assert outcome.exit_code == 0
    assert outcome.state == "success"
    assert outcome.artifact_path is not None


def test_dead_reconcile_fails_even_when_notification_fails(
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dead reconcile must reach failed even if notification write raises/returns None."""
    from grok_worker import completion_events as ce

    shared = tmp_roots["shared"]
    monkeypatch.setenv("GROK_WORKER_CACHE_ROOT", str(shared))
    clone = tmp_roots["disposable"] / "evt-dead-notify-fail"
    clone.mkdir()
    (clone / "marker.txt").write_text("x\n", encoding="utf-8")
    meta = _write_dead_running_clone(clone, "evt-dead-notify-fail")
    monkeypatch.setattr(ce, "FileLock", _BoomNotificationLock)

    result = convert_dead_worker(
        meta, clone, reason="worker process died", shared_cache_root=shared
    )
    assert result.state == WorkerState.FAILED
    persisted = WorkerMeta.read(meta_path(clone))
    assert persisted.state == WorkerState.FAILED


def test_malformed_event_rows_are_discarded(
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reads must drop empty objects, wrong types, and missing required fields."""
    from grok_worker.completion_events import (
        completion_events_path,
        emit_completion_event,
        list_completion_events,
    )

    shared = tmp_roots["shared"]
    monkeypatch.setenv("GROK_WORKER_CACHE_ROOT", str(shared))
    good = emit_completion_event(
        task_id="good-row",
        state="success",
        artifact_path=None,
        shared_cache_root=shared,
    )
    assert good is not None

    log_path = completion_events_path(shared)
    # Append malformed / incomplete rows after a valid one.
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("{}\n")
        fh.write('{"event_id":"x"}\n')
        fh.write('{"event_id":1,"task_id":"t","state":"s","timestamp":"t","artifact_path":null}\n')
        fh.write("not-json\n")
        fh.write("[1,2,3]\n")
        fh.write(
            json.dumps(
                {
                    "event_id": "partial",
                    "task_id": "t2",
                    "state": "failed",
                    # missing timestamp + artifact_path
                }
            )
            + "\n"
        )

    events = list_completion_events(shared_cache_root=shared, after="", wait_seconds=0)
    assert len(events) == 1
    assert events[0]["task_id"] == "good-row"
    assert events[0]["event_id"] == good["event_id"]


def test_bounded_wait_returns_after_delayed_event(
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real bounded wait: delayed emit unblocks waiter before wait budget ends."""
    import threading
    import time

    from grok_worker.completion_events import emit_completion_event, list_completion_events

    shared = tmp_roots["shared"]
    monkeypatch.setenv("GROK_WORKER_CACHE_ROOT", str(shared))

    # Seed a cursor event so waiter can long-poll after it.
    first = emit_completion_event(
        task_id="wait-seed",
        state="success",
        artifact_path=None,
        shared_cache_root=shared,
    )
    assert first is not None
    cursor = str(first["event_id"])

    def delayed_emit() -> None:
        time.sleep(0.15)
        emit_completion_event(
            task_id="wait-late",
            state="failed",
            artifact_path=None,
            shared_cache_root=shared,
        )

    thread = threading.Thread(target=delayed_emit, daemon=True)
    started = time.monotonic()
    thread.start()
    matched = list_completion_events(
        shared_cache_root=shared,
        after=cursor,
        wait_seconds=2.0,
        poll_interval=0.05,
    )
    elapsed = time.monotonic() - started
    thread.join(timeout=2.0)
    assert matched, "bounded wait must return the delayed event"
    assert matched[0]["task_id"] == "wait-late"
    assert elapsed < 1.5, f"wait should unblock early, elapsed={elapsed}"
    assert elapsed >= 0.1, f"wait must actually block until event, elapsed={elapsed}"
