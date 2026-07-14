"""Active PID/lock preservation and dead creating/running/finalizing conversion."""

from __future__ import annotations

import os
from pathlib import Path

from grok_worker.constants import MANAGED_BY, SCHEMA_VERSION
from grok_worker.gc import convert_dead_running, gc_disposable_root, is_active
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import meta_dir, meta_path
from grok_worker.process_identity import process_start_token


def _write_state(
    clone: Path,
    state: WorkerState,
    *,
    pid: int | None,
    token: str | None = None,
) -> WorkerMeta:
    now = utc_now()
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id=clone.name,
        source_realpath="/tmp/src",
        clone_realpath=str(clone.resolve()),
        state=state,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        runner_pid=pid,
        runner_start_token=token,
        pid=pid,
    )
    meta_dir(clone).mkdir(parents=True, exist_ok=True)
    meta.write(meta_path(clone))
    return meta


def test_active_pid_preserved(tmp_roots: dict[str, Path]) -> None:
    clone = tmp_roots["disposable"] / "live1"
    clone.mkdir()
    (clone / "x").write_text("1", encoding="utf-8")
    pid = os.getpid()
    token = process_start_token(pid)
    meta = _write_state(clone, WorkerState.RUNNING, pid=pid, token=token)
    assert is_active(meta, clone)
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert clone.name not in report.removed
    assert clone.is_dir()


def test_dead_running_converted_not_deleted(tmp_roots: dict[str, Path]) -> None:
    clone = tmp_roots["disposable"] / "dead1"
    clone.mkdir()
    (clone / "x").write_text("1", encoding="utf-8")
    _write_state(clone, WorkerState.RUNNING, pid=999_999_999, token="not-a-real-token")
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert clone.name in report.converted_dead
    assert clone.name not in report.removed
    assert clone.is_dir()
    meta = WorkerMeta.read(meta_path(clone))
    assert meta.state == WorkerState.FAILED
    assert meta.retention_deadline is not None


def test_convert_dead_running_helper(tmp_roots: dict[str, Path]) -> None:
    clone = tmp_roots["disposable"] / "dead2"
    clone.mkdir()
    meta = _write_state(clone, WorkerState.RUNNING, pid=999_999_998, token="x")
    out = convert_dead_running(meta, clone)
    assert out.state == WorkerState.FAILED


def test_pid_identity_mismatch_treated_dead(tmp_roots: dict[str, Path]) -> None:
    clone = tmp_roots["disposable"] / "reuse1"
    clone.mkdir()
    (clone / "x").write_text("1", encoding="utf-8")
    # Live pid but wrong start token → not active
    pid = os.getpid()
    meta = _write_state(clone, WorkerState.RUNNING, pid=pid, token="wrong-token-xyz")
    assert not is_active(meta, clone)
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert clone.name in report.converted_dead
    assert clone.is_dir()


def test_held_lock_preserves_even_without_pid(tmp_roots: dict[str, Path]) -> None:
    clone = tmp_roots["disposable"] / "locked1"
    clone.mkdir()
    (clone / "x").write_text("1", encoding="utf-8")
    meta = _write_state(clone, WorkerState.RUNNING, pid=None, token=None)
    # Hold worker lock in this process
    from grok_worker.locks import worker_lock

    lock = worker_lock(meta_dir(clone))
    lock.acquire()
    try:
        assert is_active(meta, clone)
        report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
        assert clone.name not in report.removed
        assert clone.is_dir()
    finally:
        lock.release()


def test_dead_creating_converted(tmp_roots: dict[str, Path]) -> None:
    clone = tmp_roots["disposable"] / "creating1"
    clone.mkdir()
    _write_state(clone, WorkerState.CREATING, pid=999_999_997, token="dead")
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert clone.name in report.converted_dead
    meta = WorkerMeta.read(meta_path(clone))
    assert meta.state == WorkerState.FAILED


def test_dead_finalizing_converted(tmp_roots: dict[str, Path]) -> None:
    clone = tmp_roots["disposable"] / "final1"
    clone.mkdir()
    _write_state(clone, WorkerState.FINALIZING, pid=999_999_996, token="dead")
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert clone.name in report.converted_dead
    meta = WorkerMeta.read(meta_path(clone))
    assert meta.state == WorkerState.FAILED
