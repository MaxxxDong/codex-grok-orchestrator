"""Concurrency contract for shared cache-domain leases under EX preflight.

On Darwin, blocking flock(LOCK_SH) waiters are woken one-at-a-time (exclusive
FIFO) after any exclusive holder. That serializes worker shared leases whenever
ensure_cache_capacity's EX|NB races with concurrent SH.acquire(), matching the
observed multi-worker cache-domain stall. Shared acquire must allow multiple
holders simultaneously even after exclusive contention.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path


def _ex_then_sh_worker(
    i: int,
    path: str,
    barrier: mp.synchronize.Barrier,
    hold_s: float,
    result_q: mp.Queue,
) -> None:
    from grok_worker.locks import FileLock

    barrier.wait(timeout=30)
    ex = FileLock(Path(path), shared=False)
    if ex.try_acquire():
        time.sleep(0.05)
        ex.release()
    sh = FileLock(Path(path), shared=True)
    t0 = time.time()
    sh.acquire()
    got = time.time()
    time.sleep(hold_s)
    rel = time.time()
    sh.release()
    result_q.put({"i": i, "wait_ms": (got - t0) * 1000, "got": got, "rel": rel})


def _ensure_then_sh_worker(
    i: int,
    root: str,
    barrier: mp.synchronize.Barrier,
    hold_s: float,
    result_q: mp.Queue,
) -> None:
    from grok_worker.cache_policy import CachePolicy, cache_use_lease, ensure_cache_capacity

    root_p = Path(root)
    barrier.wait(timeout=30)
    ensure_cache_capacity(CachePolicy(root=root_p, max_bytes=10**12, ttl_hours=720))
    lease = cache_use_lease(root_p)
    t0 = time.time()
    lease.acquire()
    got = time.time()
    time.sleep(hold_s)
    rel = time.time()
    lease.release()
    result_q.put({"i": i, "wait_ms": (got - t0) * 1000, "got": got, "rel": rel})


def _max_concurrent(results: list[dict]) -> int:
    events: list[tuple[float, int]] = []
    for result in results:
        events.append((result["got"], 1))
        events.append((result["rel"], -1))
    events.sort()
    current = maximum = 0
    for _, delta in events:
        current += delta
        maximum = max(maximum, current)
    return maximum


def test_shared_leases_overlap_after_exclusive_preflight(tmp_path: Path) -> None:
    lock_path = tmp_path / "domain.lock"
    lock_path.write_text("")
    count = 5
    hold_s = 0.4
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(count)
    result_q: mp.Queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_ex_then_sh_worker,
            args=(index, str(lock_path), barrier, hold_s, result_q),
        )
        for index in range(count)
    ]
    started = time.time()
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert not process.is_alive(), f"worker stuck pid={process.pid}"
    wall = time.time() - started
    results = [result_q.get(timeout=5) for _ in range(count)]
    maximum = _max_concurrent(results)
    assert maximum >= 3, (
        f"shared leases serialized after EX preflight: max_concurrent={maximum}, "
        f"wall_s={wall:.3f}, waits={[round(r['wait_ms'], 1) for r in results]}"
    )
    assert wall < hold_s * count * 0.7


def test_ensure_cache_capacity_then_shared_leases_concurrent(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    (root / "context-packs").mkdir()
    count = 5
    hold_s = 0.4
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(count)
    result_q: mp.Queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_ensure_then_sh_worker,
            args=(index, str(root), barrier, hold_s, result_q),
        )
        for index in range(count)
    ]
    started = time.time()
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert not process.is_alive(), f"worker stuck pid={process.pid}"
    wall = time.time() - started
    results = [result_q.get(timeout=5) for _ in range(count)]
    maximum = _max_concurrent(results)
    assert maximum >= 3, (
        f"ensure+shared serialized: max_concurrent={maximum}, wall_s={wall:.3f}, "
        f"waits={[round(r['wait_ms'], 1) for r in results]}"
    )
    assert wall < hold_s * count * 0.7


def test_gc_still_defers_while_shared_lease_held(tmp_path: Path) -> None:
    from grok_worker.cache_policy import CachePolicy, cache_use_lease, gc_shared_cache

    root = tmp_path / "cache"
    stale = root / "context-packs" / "stale.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")
    now = time.time()
    os.utime(stale, (now - 10_000, now - 10_000))
    with cache_use_lease(root):
        report = gc_shared_cache(CachePolicy(root=root, max_bytes=1, ttl_hours=1), now=now)
    assert stale.exists()
    assert "active-cache-users" in report.protected
