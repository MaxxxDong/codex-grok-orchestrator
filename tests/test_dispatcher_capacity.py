"""OS flock slot leases: multiprocess barriers, no preemption, crash release.

Uses temp shared cache and FileLock only — no real Grok sessions.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest

from grok_worker.constants import (
    DISPATCHER_CONCURRENCY_BUSY,
    MANAGED_BY,
    MAX_CONCURRENT_WORKERS,
    SCHEMA_VERSION,
)
from grok_worker.dispatcher import (
    DispatcherConcurrencyError,
    SameSourceConflictError,
    count_held_slots,
    hash_identity,
    reserve_dispatcher_capacity,
    slot_lock_path,
    try_acquire_slot,
    try_acquire_source_lock,
)
from grok_worker.locks import FileLock
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import meta_path


def test_max_concurrent_default_remains_10() -> None:
    assert MAX_CONCURRENT_WORKERS == 10


def test_slot_lock_paths_are_fixed_under_dispatcher_hash(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    did = "dispatcher-alpha"
    dig = hash_identity(did)
    p0 = slot_lock_path(shared, did, 0)
    p9 = slot_lock_path(shared, did, 9)
    assert dig in str(p0)
    assert p0.name == "00.lock"
    assert p9.name == "09.lock"
    assert "slots" in p0.parts
    with pytest.raises(ValueError):
        slot_lock_path(shared, did, 10)


def test_ten_slots_block_eleventh(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    did = "cap-d"
    held: list[FileLock] = []
    for _ in range(10):
        held.append(try_acquire_slot(shared, did, limit=10))
    assert count_held_slots(shared, did) == 10
    with pytest.raises(DispatcherConcurrencyError) as excinfo:
        try_acquire_slot(shared, did, limit=10)
    assert excinfo.value.code == DISPATCHER_CONCURRENCY_BUSY
    assert DISPATCHER_CONCURRENCY_BUSY in str(excinfo.value)
    assert excinfo.value.active == 10
    assert excinfo.value.limit == 10
    for lock in held:
        lock.release()
    assert count_held_slots(shared, did) == 0
    free = try_acquire_slot(shared, did, limit=10)
    free.release()


def test_other_dispatcher_does_not_block(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    held = [try_acquire_slot(shared, "other-disp", limit=10) for _ in range(10)]
    # Different dispatcher has full budget.
    mine = reserve_dispatcher_capacity(shared, "mine", mode="analysis")
    assert count_held_slots(shared, "mine") == 1
    mine.release()
    for lock in held:
        lock.release()


def test_session_open_meta_does_not_consume_flock_slot(tmp_path: Path) -> None:
    """Idle SESSION_OPEN lifecycle rows must not hold OS slot capacity."""
    shared = tmp_path / "shared"
    shared.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    for i in range(10):
        clone = root / f"s{i}"
        clone.mkdir()
        now = utc_now()
        meta = WorkerMeta(
            schema_version=SCHEMA_VERSION,
            task_id=f"s{i}",
            source_realpath="/tmp/src",
            clone_realpath=str(clone.resolve()),
            state=WorkerState.SESSION_OPEN,
            created_at=dt_to_iso(now) or "",
            updated_at=dt_to_iso(now) or "",
            managed_by=MANAGED_BY,
            dispatcher_id="sess",
            mode="analysis",
        )
        from grok_worker.paths import meta_dir

        meta_dir(clone).mkdir(parents=True, exist_ok=True)
        meta.write(meta_path(clone))
    # All ten OS slots still free.
    assert count_held_slots(shared, "sess") == 0
    lease = reserve_dispatcher_capacity(shared, "sess", mode="analysis")
    lease.release()


def test_same_source_implementation_exclusion_via_flock(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    src = str((tmp_path / "canonical-src").resolve())
    (tmp_path / "canonical-src").mkdir()
    first = reserve_dispatcher_capacity(
        shared, "d1", mode="implementation", source_realpath=src
    )
    with pytest.raises(SameSourceConflictError) as excinfo:
        reserve_dispatcher_capacity(
            shared, "d1", mode="implementation", source_realpath=src
        )
    assert "source_hash" in str(excinfo.value) or excinfo.value.source_hash
    # Analysis may coexist (no source lock).
    analysis = reserve_dispatcher_capacity(
        shared, "d1", mode="analysis", source_realpath=src
    )
    analysis.release()
    first.release()
    second = reserve_dispatcher_capacity(
        shared, "d1", mode="implementation", source_realpath=src
    )
    second.release()


def test_different_sources_not_blocked(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    a = str((tmp_path / "src-a").resolve())
    b = str((tmp_path / "src-b").resolve())
    (tmp_path / "src-a").mkdir()
    (tmp_path / "src-b").mkdir()
    la = reserve_dispatcher_capacity(
        shared, "d1", mode="implementation", source_realpath=a
    )
    lb = reserve_dispatcher_capacity(
        shared, "d1", mode="implementation", source_realpath=b
    )
    la.release()
    lb.release()


def test_different_dispatchers_same_source_do_not_block(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    src = str((tmp_path / "src").resolve())
    (tmp_path / "src").mkdir()
    a = reserve_dispatcher_capacity(
        shared, "disp-a", mode="implementation", source_realpath=src
    )
    b = reserve_dispatcher_capacity(
        shared, "disp-b", mode="implementation", source_realpath=src
    )
    a.release()
    b.release()


def test_lease_releases_on_exception_finally(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    did = "exc-d"
    try:
        with reserve_dispatcher_capacity(shared, did, mode="analysis") as lease:
            assert count_held_slots(shared, did) == 1
            assert lease.slot is not None
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert count_held_slots(shared, did) == 0
    again = try_acquire_slot(shared, did)
    again.release()


def test_root_scoped_without_dispatcher_id_still_limits_one_root(
    tmp_roots: dict[str, Path],
) -> None:
    from grok_worker.capacity import ConcurrencyError, count_active_workers, enforce_concurrency

    for i in range(MAX_CONCURRENT_WORKERS):
        c = tmp_roots["disposable"] / f"w{i}"
        c.mkdir()
        now = utc_now()
        meta = WorkerMeta(
            schema_version=SCHEMA_VERSION,
            task_id=f"w{i}",
            source_realpath="/tmp/s",
            clone_realpath=str(c.resolve()),
            state=WorkerState.RUNNING,
            created_at=dt_to_iso(now) or "",
            updated_at=dt_to_iso(now) or "",
            managed_by=MANAGED_BY,
        )
        meta.write(meta_path(c))
    assert count_active_workers(tmp_roots["disposable"]) == 10
    with pytest.raises(ConcurrencyError):
        enforce_concurrency(tmp_roots["disposable"], 10)


def test_session_open_not_in_root_scoped_active_count(tmp_roots: dict[str, Path]) -> None:
    from grok_worker.capacity import count_active_workers, enforce_concurrency

    for i in range(10):
        c = tmp_roots["disposable"] / f"open{i}"
        c.mkdir()
        now = utc_now()
        meta = WorkerMeta(
            schema_version=SCHEMA_VERSION,
            task_id=f"open{i}",
            source_realpath="/tmp/s",
            clone_realpath=str(c.resolve()),
            state=WorkerState.SESSION_OPEN,
            created_at=dt_to_iso(now) or "",
            updated_at=dt_to_iso(now) or "",
            managed_by=MANAGED_BY,
        )
        meta.write(meta_path(c))
    assert count_active_workers(tmp_roots["disposable"]) == 0
    assert enforce_concurrency(tmp_roots["disposable"], 10) == 0


# --- Multiprocess barrier tests ------------------------------------------------


def _slot_worker(
    i: int,
    shared: str,
    did: str,
    barrier: mp.synchronize.Barrier,
    hold_s: float,
    result_q: mp.Queue,
) -> None:
    from grok_worker.dispatcher import DispatcherConcurrencyError, try_acquire_slot

    barrier.wait(timeout=30)
    try:
        lock = try_acquire_slot(Path(shared), did, limit=10)
    except DispatcherConcurrencyError as exc:
        result_q.put({"i": i, "ok": False, "code": exc.code, "active": exc.active})
        return
    t0 = time.time()
    time.sleep(hold_s)
    lock.release()
    result_q.put({"i": i, "ok": True, "held_s": time.time() - t0})


def test_multiprocess_two_roots_cannot_oversubscribe_final_slot(tmp_path: Path) -> None:
    """Eleven concurrent processes: exactly ten acquire, one gets BUSY."""
    shared = tmp_path / "shared"
    shared.mkdir()
    did = "mp-slots"
    n = 11
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(n)
    result_q: mp.Queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=_slot_worker,
            args=(i, str(shared), did, barrier, 0.4, result_q),
        )
        for i in range(n)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0
    results = [result_q.get(timeout=5) for _ in range(n)]
    ok = [r for r in results if r["ok"]]
    busy = [r for r in results if not r["ok"]]
    assert len(ok) == 10
    assert len(busy) == 1
    assert busy[0]["code"] == DISPATCHER_CONCURRENCY_BUSY
    assert busy[0]["active"] == 10


def _impl_source_worker(
    i: int,
    shared: str,
    did: str,
    source: str,
    barrier: mp.synchronize.Barrier,
    hold_s: float,
    result_q: mp.Queue,
) -> None:
    from grok_worker.dispatcher import (
        SameSourceConflictError,
        reserve_dispatcher_capacity,
    )

    barrier.wait(timeout=30)
    try:
        lease = reserve_dispatcher_capacity(
            Path(shared),
            did,
            mode="implementation",
            source_realpath=source,
        )
    except SameSourceConflictError as exc:
        result_q.put({"i": i, "ok": False, "err": "source", "hash": exc.source_hash})
        return
    except Exception as exc:  # noqa: BLE001
        result_q.put({"i": i, "ok": False, "err": type(exc).__name__})
        return
    time.sleep(hold_s)
    lease.release()
    result_q.put({"i": i, "ok": True})


def test_multiprocess_same_source_implementation_exclusive(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    source = str(src.resolve())
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    result_q: mp.Queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=_impl_source_worker,
            args=(i, str(shared), "d-src", source, barrier, 0.5, result_q),
        )
        for i in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0
    results = [result_q.get(timeout=5) for _ in range(2)]
    ok = [r for r in results if r["ok"]]
    fail = [r for r in results if not r["ok"]]
    assert len(ok) == 1
    assert len(fail) == 1
    assert fail[0]["err"] == "source"
    # No raw path in error payload.
    assert source not in str(fail[0])


def _crash_holder(shared: str, did: str, ready: mp.synchronize.Event) -> None:
    from grok_worker.dispatcher import try_acquire_slot

    lock = try_acquire_slot(Path(shared), did, limit=10)
    # Deliberately do not release — process exit must free the OS flock.
    ready.set()
    time.sleep(60)
    # Unreachable if parent kills us; keep lock ref alive.
    _ = lock


def test_os_process_exit_releases_slot_for_reuse(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    did = "crash-d"
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    p = ctx.Process(target=_crash_holder, args=(str(shared), did, ready))
    p.start()
    assert ready.wait(timeout=15)
    # While holder lives, slot budget is reduced.
    held_before = count_held_slots(shared, did)
    assert held_before >= 1
    p.kill()
    p.join(timeout=15)
    assert p.exitcode is not None
    # After process exit, OS releases flock; all slots reusable.
    deadline = time.time() + 5
    while time.time() < deadline:
        if count_held_slots(shared, did) == 0:
            break
        time.sleep(0.05)
    assert count_held_slots(shared, did) == 0
    for _ in range(10):
        lock = try_acquire_slot(shared, did, limit=10)
        lock.release()


def test_source_lock_filename_has_no_raw_path(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    src = tmp_path / "my-secret-path-name"
    src.mkdir()
    path_s = str(src.resolve())
    lock = try_acquire_source_lock(shared, "d1", path_s)
    assert "my-secret-path-name" not in str(lock.path)
    assert hash_identity(path_s) in lock.path.name
    lock.release()


# --- Symlink-safe lock namespaces ----------------------------------------------


def test_shared_cache_root_symlink_fail_closed(tmp_path: Path) -> None:
    from grok_worker.dispatcher import DispatcherPathError

    real_cache = tmp_path / "real-cache"
    real_cache.mkdir()
    shared = tmp_path / "shared-link"
    shared.symlink_to(real_cache)
    with pytest.raises(DispatcherPathError, match="shared cache root is a symlink"):
        try_acquire_slot(shared, "sym-cache", limit=10)


def test_dispatchers_directory_symlink_fail_closed(tmp_path: Path) -> None:
    from grok_worker.constants import DISPATCHER_REGISTRY_DIR
    from grok_worker.dispatcher import DispatcherPathError

    shared = tmp_path / "shared"
    shared.mkdir()
    outside = tmp_path / "outside-dispatchers"
    outside.mkdir()
    (shared / DISPATCHER_REGISTRY_DIR).symlink_to(outside)
    with pytest.raises(DispatcherPathError, match="symlink"):
        try_acquire_slot(shared, "sym-disp", limit=10)
    # Errors must not leak secret-like destinations.
    try:
        try_acquire_slot(shared, "sym-disp", limit=10)
    except DispatcherPathError as exc:
        assert "outside-dispatchers" not in str(exc)
        assert "token" not in str(exc).lower()


def test_slots_directory_symlink_fail_closed(tmp_path: Path) -> None:
    from grok_worker.constants import DISPATCHER_REGISTRY_DIR
    from grok_worker.dispatcher import DispatcherPathError, hash_identity

    shared = tmp_path / "shared"
    shared.mkdir()
    dig = hash_identity("sym-slots")
    ddir = shared / DISPATCHER_REGISTRY_DIR / dig
    ddir.mkdir(parents=True)
    outside = tmp_path / "evil-slots"
    outside.mkdir()
    (ddir / "slots").symlink_to(outside)
    with pytest.raises(DispatcherPathError, match="symlink"):
        try_acquire_slot(shared, "sym-slots", limit=10)


def test_sources_directory_symlink_fail_closed(tmp_path: Path) -> None:
    from grok_worker.constants import DISPATCHER_REGISTRY_DIR
    from grok_worker.dispatcher import DispatcherPathError, hash_identity

    shared = tmp_path / "shared"
    shared.mkdir()
    dig = hash_identity("sym-src")
    ddir = shared / DISPATCHER_REGISTRY_DIR / dig
    ddir.mkdir(parents=True)
    outside = tmp_path / "evil-sources"
    outside.mkdir()
    (ddir / "sources").symlink_to(outside)
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(DispatcherPathError, match="symlink"):
        try_acquire_source_lock(shared, "sym-src", str(src.resolve()))


def test_individual_slot_lock_symlink_fail_closed(tmp_path: Path) -> None:
    from grok_worker.dispatcher import DispatcherPathError

    shared = tmp_path / "shared"
    shared.mkdir()
    # Create a valid slots dir, then plant a symlink at 00.lock.
    path = slot_lock_path(shared, "sym-lock", 0)
    if path.exists():
        path.unlink()
    victim = tmp_path / "victim.lock"
    victim.write_text("secret-token-xyz\n", encoding="utf-8")
    path.symlink_to(victim)
    with pytest.raises((DispatcherPathError, RuntimeError), match="symlink"):
        try_acquire_slot(shared, "sym-lock", limit=10)
    # Victim content must not appear in error text.
    try:
        try_acquire_slot(shared, "sym-lock", limit=10)
    except (DispatcherPathError, RuntimeError) as exc:
        assert "secret-token-xyz" not in str(exc)
        assert "victim.lock" not in str(exc)


def test_individual_source_lock_symlink_fail_closed(tmp_path: Path) -> None:
    from grok_worker.dispatcher import DispatcherPathError, source_lock_path

    shared = tmp_path / "shared"
    shared.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    path_s = str(src.resolve())
    sh = hash_identity(path_s)
    path = source_lock_path(shared, "sym-srclock", sh)
    if path.exists():
        path.unlink()
    victim = tmp_path / "src-victim.lock"
    victim.write_text("env-secret\n", encoding="utf-8")
    path.symlink_to(victim)
    with pytest.raises((DispatcherPathError, RuntimeError), match="symlink"):
        try_acquire_source_lock(shared, "sym-srclock", path_s)


def test_hash_dir_symlink_fail_closed(tmp_path: Path) -> None:
    from grok_worker.constants import DISPATCHER_REGISTRY_DIR
    from grok_worker.dispatcher import DispatcherPathError, hash_identity

    shared = tmp_path / "shared"
    shared.mkdir()
    reg = shared / DISPATCHER_REGISTRY_DIR
    reg.mkdir()
    dig = hash_identity("sym-hash")
    outside = tmp_path / "escaped-hash"
    outside.mkdir()
    (reg / dig).symlink_to(outside)
    with pytest.raises(DispatcherPathError, match="symlink"):
        try_acquire_slot(shared, "sym-hash", limit=10)
