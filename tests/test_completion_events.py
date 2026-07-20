"""Completion-event notification seam (shared cache JSONL + events CLI).

Public contract:
- Terminal-state transitions append an immediate terminal event; one-shot cleanup
  appends a distinct settled event.
- ``grok-worker watch`` waits for events and returns compact health only on timeout.
- Events carry only non-sensitive pointers (task_id/state/artifact_path/
  timestamp/event_id); lifecycle files remain the source of truth.
- Repeated finalize/reconcile must not emit duplicate notifications.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from grok_worker.cli import main
from grok_worker.completion_events import emit_completion_event, list_completion_events
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
REQUIRED_EVENT_KEYS = frozenset({"event_id", "task_id", "state", "timestamp", "artifact_path"})
# New optional pointer fields (present on new emits when known).
OPTIONAL_EVENT_KEYS = frozenset(
    {
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
        runner_start_token="not-" + "a-real-token",
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
    """Happy path: success emits immediate terminal then cleanup-settled."""
    shared = tmp_roots["shared"]
    monkeypatch.setenv("GROK_WORKER_CACHE_ROOT", str(shared))
    cfg = RunConfig(
        source=git_source,
        prompt="emit completion event",
        backend="acp",
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
    assert len(matching) == 2
    assert [event.get("kind") for event in matching] == ["terminal", "settled"]
    terminal, settled = matching
    _assert_event_shape(terminal)
    _assert_event_shape(settled)
    assert terminal["state"] == "success"
    assert settled["state"] == "success"
    assert settled["artifact_ready"] is True
    assert settled["clone_cleaned"] is True
    assert settled["session_cleaned"] is True
    assert settled["attention_required"] is False
    if outcome.artifact_path:
        assert terminal["artifact_path"] == outcome.artifact_path
        assert settled["artifact_path"] == outcome.artifact_path

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
    assert any(isinstance(item, dict) and item.get("task_id") == "evt-ok-01" for item in listed), (
        f"events CLI JSON must include the completion event; got {payload!r}"
    )


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
        f"reconcile terminal transition must write shared-cache notification log at {log_path}"
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
        backend="acp",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=shared,
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        task_id="evt-notify-io-fail",
    )
    outcome = run_worker(cfg)
    assert outcome.exit_code == 0
    assert outcome.state == "success"
    assert outcome.artifact_path is not None
    assert outcome.clone_path is not None
    assert Path(outcome.clone_path).is_dir()
    assert "notification was not durable" in outcome.message


def test_run_event_receipt_survives_global_index_failure(
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from grok_worker import completion_events as ce

    shared = tmp_roots["shared"]
    blocked = shared / "notifications" / "blocked-global"
    blocked.mkdir(parents=True)
    monkeypatch.setattr(ce, "completion_events_path", lambda _root: blocked)

    event = ce.emit_completion_event(
        task_id="receipt-task",
        state="success",
        run_id="receipt-run",
        kind="terminal",
        shared_cache_root=shared,
    )

    assert event is not None
    assert ce.run_completion_events_path(shared, "receipt-run").is_file()
    listed = ce.list_completion_events(
        shared_cache_root=shared,
        run_id="receipt-run",
        wait_seconds=0,
    )
    assert [item["kind"] for item in listed] == ["terminal"]


def test_wait_does_not_reparse_unchanged_full_log(
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_roots["shared"]
    emit_completion_event(
        task_id="existing",
        state="success",
        run_id="existing-run",
        shared_cache_root=shared,
    )
    log_path = _notification_log(shared).resolve()
    original = Path.read_text
    reads = 0

    def counting_read_text(path: Path, *args: object, **kwargs: object) -> str:
        nonlocal reads
        if path.resolve() == log_path:
            reads += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    assert not list_completion_events(
        shared_cache_root=shared,
        run_id="missing-run",
        wait_seconds=0.2,
        poll_interval=0.01,
    )
    assert reads <= 2


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


def test_emit_dedupes_by_run_id_not_only_task_state(
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from grok_worker.completion_events import emit_completion_event, list_completion_events

    shared = tmp_roots["shared"]
    monkeypatch.setenv("GROK_WORKER_CACHE_ROOT", str(shared))
    a = emit_completion_event(
        task_id="same-task",
        state="success",
        run_id="run-aaa",
        shared_cache_root=shared,
    )
    b = emit_completion_event(
        task_id="same-task",
        state="success",
        run_id="run-bbb",
        shared_cache_root=shared,
    )
    assert a is not None and b is not None
    again = emit_completion_event(
        task_id="same-task",
        state="success",
        run_id="run-aaa",
        shared_cache_root=shared,
    )
    assert again is None
    events = list_completion_events(shared_cache_root=shared, wait_seconds=0)
    matching = [e for e in events if e.get("task_id") == "same-task"]
    assert len(matching) == 2
    assert {e["run_id"] for e in matching} == {"run-aaa", "run-bbb"}


def test_terminal_and_settled_have_independent_dedup_keys(
    tmp_roots: dict[str, Path],
) -> None:
    from grok_worker.completion_events import emit_completion_event, list_completion_events

    shared = tmp_roots["shared"]
    terminal = emit_completion_event(
        task_id="same-run",
        state="success",
        run_id="run-one",
        kind="terminal",
        shared_cache_root=shared,
    )
    settled = emit_completion_event(
        task_id="same-run",
        state="success",
        run_id="run-one",
        kind="settled",
        clone_cleaned=True,
        session_cleaned=True,
        shared_cache_root=shared,
    )
    duplicate_settled = emit_completion_event(
        task_id="same-run",
        state="success",
        run_id="run-one",
        kind="settled",
        shared_cache_root=shared,
    )
    assert terminal is not None and settled is not None
    assert duplicate_settled is None
    events = list_completion_events(shared_cache_root=shared, run_id="run-one")
    assert [event["kind"] for event in events] == ["terminal", "settled"]


def test_attention_dedup_preserves_distinct_reasons(
    tmp_roots: dict[str, Path],
) -> None:
    from grok_worker.completion_events import emit_completion_event, list_completion_events

    shared = tmp_roots["shared"]
    first = emit_completion_event(
        task_id="attention-task",
        state="running",
        run_id="attention-run",
        kind="attention",
        reason_code="provider_http_5xx",
        shared_cache_root=shared,
    )
    second = emit_completion_event(
        task_id="attention-task",
        state="running",
        run_id="attention-run",
        kind="attention",
        reason_code="no_productive_progress",
        shared_cache_root=shared,
    )
    duplicate = emit_completion_event(
        task_id="attention-task",
        state="running",
        run_id="attention-run",
        kind="attention",
        reason_code="no_productive_progress",
        shared_cache_root=shared,
    )
    assert first is not None and second is not None
    assert duplicate is None
    events = list_completion_events(shared_cache_root=shared, run_id="attention-run")
    assert [event["reason_code"] for event in events] == [
        "provider_http_5xx",
        "no_productive_progress",
    ]


def test_list_events_filter_by_run_id_and_dispatcher(
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from grok_worker.completion_events import emit_completion_event, list_completion_events

    shared = tmp_roots["shared"]
    monkeypatch.setenv("GROK_WORKER_CACHE_ROOT", str(shared))
    emit_completion_event(
        task_id="t1",
        state="success",
        run_id="r1",
        dispatcher_id="d1",
        shared_cache_root=shared,
    )
    emit_completion_event(
        task_id="t2",
        state="failed",
        run_id="r2",
        dispatcher_id="d2",
        shared_cache_root=shared,
    )
    only_r1 = list_completion_events(shared_cache_root=shared, run_id="r1", wait_seconds=0)
    assert len(only_r1) == 1 and only_r1[0]["run_id"] == "r1"
    only_d2 = list_completion_events(shared_cache_root=shared, dispatcher_id="d2", wait_seconds=0)
    assert len(only_d2) == 1 and only_d2[0]["dispatcher_id"] == "d2"
    # Unfiltered remains compatible.
    all_events = list_completion_events(shared_cache_root=shared, wait_seconds=0)
    assert len(all_events) >= 2


def test_events_wait_bounds_default_and_reject(
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from grok_worker.completion_events import (
        DEFAULT_EVENT_WAIT_SECONDS,
        MAX_EVENT_WAIT_SECONDS,
        EventWaitError,
        validate_wait_seconds,
    )

    assert DEFAULT_EVENT_WAIT_SECONDS == 30
    assert MAX_EVENT_WAIT_SECONDS == 600
    assert validate_wait_seconds(0) == 0
    assert validate_wait_seconds(30) == 30
    assert validate_wait_seconds(120) == 120
    assert validate_wait_seconds(300) == 300
    assert validate_wait_seconds(600) == 600
    with pytest.raises(EventWaitError):
        validate_wait_seconds(-1)
    with pytest.raises(EventWaitError):
        validate_wait_seconds(601)

    shared = tmp_roots["shared"]
    monkeypatch.setenv("GROK_WORKER_CACHE_ROOT", str(shared))
    code = main(
        [
            "events",
            "--shared-cache-root",
            str(shared),
            "--wait-seconds",
            "601",
            "--json",
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "600" in err or "wait" in err.lower()


def test_watch_unblocks_on_delayed_attention_event(
    tmp_roots: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    import threading
    import time

    from grok_worker.completion_events import emit_completion_event

    shared = tmp_roots["shared"]

    def delayed_emit() -> None:
        time.sleep(0.15)
        emit_completion_event(
            task_id="watch-failed",
            state="failed",
            timestamp="2000-01-01T00:00:00+00:00",
            run_id="watch-run-1",
            dispatcher_id="watch-disp",
            kind="terminal",
            attention_required=True,
            shared_cache_root=shared,
        )

    thread = threading.Thread(target=delayed_emit, daemon=True)
    started = time.monotonic()
    thread.start()
    code = main(
        [
            "watch",
            "--shared-cache-root",
            str(shared),
            "--disposable-root",
            str(tmp_roots["disposable"]),
            "--run-id",
            "watch-run-1",
            "--wait-seconds",
            "2",
            "--json",
        ]
    )
    elapsed = time.monotonic() - started
    thread.join(timeout=2)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "events"
    assert payload["attention_required"] is True
    assert payload["events"][0]["task_id"] == "watch-failed"
    assert payload["events"][0]["timestamp"].startswith("2000-01-01")
    assert payload["events"][0]["emitted_at"].startswith("20")
    assert payload["events"][0]["watch_delivery_latency_seconds"] < 1.0
    assert elapsed < 1.5


def test_watch_until_settled_keeps_one_wait_through_terminal_cleanup(
    tmp_roots: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    import threading
    import time

    shared = tmp_roots["shared"]

    def delayed_emit() -> None:
        time.sleep(0.1)
        emit_completion_event(
            task_id="watch-settle",
            state="success",
            run_id="watch-run-settle",
            kind="terminal",
            shared_cache_root=shared,
        )
        time.sleep(0.1)
        emit_completion_event(
            task_id="watch-settle",
            state="success",
            run_id="watch-run-settle",
            kind="settled",
            clone_cleaned=True,
            session_cleaned=True,
            shared_cache_root=shared,
        )

    thread = threading.Thread(target=delayed_emit, daemon=True)
    thread.start()
    code = main(
        [
            "watch",
            "--shared-cache-root",
            str(shared),
            "--disposable-root",
            str(tmp_roots["disposable"]),
            "--run-id",
            "watch-run-settle",
            "--wait-seconds",
            "2",
            "--until-settled",
            "--json",
        ]
    )
    thread.join(timeout=2)

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["settled"] is True
    assert [event["kind"] for event in payload["events"]] == ["terminal", "settled"]


def test_watch_until_settled_requires_one_run(
    tmp_roots: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "watch",
            "--shared-cache-root",
            str(tmp_roots["shared"]),
            "--disposable-root",
            str(tmp_roots["disposable"]),
            "--dispatcher-id",
            "wave",
            "--until-settled",
            "--wait-seconds",
            "0",
        ]
    )

    assert code == 2
    assert "requires --run-id" in capsys.readouterr().err


def test_watch_surfaces_terminal_lifecycle_when_notification_is_missing(
    tmp_roots: dict[str, Path],
) -> None:
    from grok_worker.watch import watch_workers

    clone = tmp_roots["disposable"] / "watch-missing-event"
    clone.mkdir()
    meta = _write_dead_running_clone(clone, "watch-missing-event")
    meta.run_id = "watch-missing-event-run"
    meta.write(meta_path(clone))

    def finish_without_event() -> None:
        time.sleep(0.1)
        current = WorkerMeta.read(meta_path(clone))
        current.state = WorkerState.SUCCESS
        current.runner_pid = None
        current.runner_start_token = None
        current.pid = None
        current.artifact_complete = True
        current.touch()
        current.write(meta_path(clone))

    thread = threading.Thread(target=finish_without_event)
    thread.start()
    started = time.monotonic()
    payload = watch_workers(
        shared_cache_root=tmp_roots["shared"],
        disposable_root=tmp_roots["disposable"],
        wait_seconds=2,
        run_id="watch-missing-event-run",
    )
    elapsed = time.monotonic() - started
    thread.join(timeout=1)

    assert elapsed < 1.5
    assert payload["kind"] == "heartbeat"
    assert payload["reason_code"] == "terminal_notification_missing"
    assert payload["workers"][0]["state"] == "success"
    assert payload["workers"][0]["terminal_event_ready"] is False


def test_watch_timeout_returns_compact_healthy_heartbeat(
    tmp_roots: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from grok_worker.process_identity import capture_identity

    clone = tmp_roots["disposable"] / "watch-running"
    clone.mkdir()
    now = utc_now()
    pid, token = capture_identity()
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="watch-running",
        source_realpath="/tmp/watch-source",
        clone_realpath=str(clone.resolve()),
        state=WorkerState.RUNNING,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        runner_pid=pid,
        runner_start_token=token,
        pid=pid,
        run_id="watch-run-healthy",
        dispatcher_id="watch-dispatcher",
        mode="analysis",
        backend="native",
    )
    meta_dir(clone).mkdir(parents=True, exist_ok=True)
    meta.write(meta_path(clone))

    code = main(
        [
            "watch",
            "--shared-cache-root",
            str(tmp_roots["shared"]),
            "--disposable-root",
            str(tmp_roots["disposable"]),
            "--run-id",
            "watch-run-healthy",
            "--wait-seconds",
            "0",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "heartbeat"
    assert payload["attention_required"] is False
    assert len(payload["workers"]) == 1
    worker = payload["workers"][0]
    assert worker["state"] == "running"
    assert worker["runner_live"] is True
    assert "runner_pid" not in worker
    assert "error_message" not in worker


def test_one_dispatcher_watch_collects_parallel_run_events(
    tmp_roots: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from grok_worker.completion_events import emit_completion_event

    shared = tmp_roots["shared"]
    for run_id in ("parallel-run-a", "parallel-run-b"):
        emit_completion_event(
            task_id=run_id,
            state="success",
            run_id=run_id,
            dispatcher_id="parallel-dispatcher",
            kind="terminal",
            shared_cache_root=shared,
        )
    emit_completion_event(
        task_id="other-run",
        state="failed",
        run_id="other-run",
        dispatcher_id="other-dispatcher",
        kind="terminal",
        shared_cache_root=shared,
    )

    code = main(
        [
            "watch",
            "--shared-cache-root",
            str(shared),
            "--disposable-root",
            str(tmp_roots["disposable"]),
            "--dispatcher-id",
            "parallel-dispatcher",
            "--wait-seconds",
            "0",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "events"
    assert payload["count"] == 2
    assert {event["run_id"] for event in payload["events"]} == {
        "parallel-run-a",
        "parallel-run-b",
    }


def test_run_startup_failure_emits_immediate_attention(
    tmp_roots: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "run",
            "--source",
            str(tmp_roots["root"] / "missing-source"),
            "--prompt",
            "must not launch",
            "--task-id",
            "startup-missing",
            "--run-id",
            "startup-run-1",
            "--dispatcher-id",
            "startup-dispatcher",
            "--disposable-root",
            str(tmp_roots["disposable"]),
            "--artifact-root",
            str(tmp_roots["artifacts"]),
            "--shared-cache-root",
            str(tmp_roots["shared"]),
            "--no-prepare-deps",
        ]
    )
    assert code == 1
    capsys.readouterr()
    events = _load_jsonl(_notification_log(tmp_roots["shared"]))
    assert len(events) == 1
    event = events[0]
    assert event["kind"] == "attention"
    assert event["state"] == "startup_failed"
    assert event["run_id"] == "startup-run-1"
    assert event["attention_required"] is True
    assert event["reason_code"] == "startup_file_not_found_error"


def test_live_provider_error_emits_attention_before_recovered_terminal(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_ACPX_BEHAVIOR", "provider_500_then_success")
    monkeypatch.setenv("FAKE_PROVIDER_RECOVERY_DELAY_SECONDS", "4")
    cfg = RunConfig(
        source=git_source,
        prompt="recover after visible provider error",
        backend="acp",
        mode="analysis",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        task_id="provider-attention",
        run_id="provider-attention-run",
        dispatcher_id="provider-attention-dispatcher",
        skip_post_gc=True,
    )
    observed: dict[str, object] = {}

    def observe() -> None:
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            events = list_completion_events(
                shared_cache_root=tmp_roots["shared"],
                wait_seconds=0,
                run_id="provider-attention-run",
            )
            attention = next(
                (event for event in events if event.get("kind") == "attention"),
                None,
            )
            if attention is not None:
                observed["attention"] = attention
                observed["at"] = time.monotonic()
                return
            time.sleep(0.05)

    observer = threading.Thread(target=observe)
    observer.start()
    outcome = run_worker(cfg)
    finished_at = time.monotonic()
    observer.join(timeout=1)

    attention = observed.get("attention")
    assert isinstance(attention, dict)
    assert float(observed["at"]) < finished_at - 1
    assert attention["state"] == "running"
    assert attention["attention_required"] is True
    assert attention["reason_code"] == "provider_http_5xx"
    assert outcome.state == "success"
    final_events = list_completion_events(
        shared_cache_root=tmp_roots["shared"],
        wait_seconds=0,
        run_id="provider-attention-run",
    )
    assert [event.get("kind") for event in final_events] == [
        "attention",
        "terminal",
        "settled",
    ]
