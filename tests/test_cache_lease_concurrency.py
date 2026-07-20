"""Concurrency contract for shared cache-domain leases under EX preflight.

On Darwin, blocking flock(LOCK_SH) waiters are woken one-at-a-time (exclusive
FIFO) after any exclusive holder. That serializes worker shared leases whenever
ensure_cache_capacity's EX|NB races with concurrent SH.acquire(), matching the
observed multi-worker cache-domain stall. Shared acquire must allow multiple
holders simultaneously even after exclusive contention.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from tests.subprocess_concurrency import run_barrier_workers


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


def _active_span(results: list[dict]) -> float:
    """Measure lease activity without including subprocess startup overhead."""
    return max(result["rel"] for result in results) - min(result["got"] for result in results)


def test_shared_leases_overlap_after_exclusive_preflight(tmp_path: Path) -> None:
    lock_path = tmp_path / "domain.lock"
    lock_path.write_text("")
    count = 5
    hold_s = 0.4
    results, wall = run_barrier_workers(
        tmp_path,
        "ex-then-sh",
        [(index, lock_path, hold_s) for index in range(count)],
    )
    maximum = _max_concurrent(results)
    assert maximum >= 3, (
        f"shared leases serialized after EX preflight: max_concurrent={maximum}, "
        f"wall_s={wall:.3f}, waits={[round(r['wait_ms'], 1) for r in results]}"
    )
    active_span = _active_span(results)
    assert active_span < hold_s * count * 0.7, (
        f"shared lease activity serialized: active_span_s={active_span:.3f}, wall_s={wall:.3f}"
    )


def test_ensure_cache_capacity_then_shared_leases_concurrent(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    (root / "context-packs").mkdir()
    count = 5
    hold_s = 0.4
    results, wall = run_barrier_workers(
        tmp_path,
        "ensure-then-sh",
        [(index, root, hold_s) for index in range(count)],
    )
    maximum = _max_concurrent(results)
    assert maximum >= 3, (
        f"ensure+shared serialized: max_concurrent={maximum}, wall_s={wall:.3f}, "
        f"waits={[round(r['wait_ms'], 1) for r in results]}"
    )
    active_span = _active_span(results)
    assert active_span < hold_s * count * 0.7, (
        f"ensure+shared activity serialized: active_span_s={active_span:.3f}, wall_s={wall:.3f}"
    )


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
