"""Cap enforcement, hidden usage, concurrency, post-clone rollback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from grok_worker.cache_policy import DEFAULT_CACHE_MAX_BYTES, DEFAULT_CACHE_TTL_HOURS
from grok_worker.capacity import (
    CapacityError,
    ConcurrencyError,
    count_active_workers,
    enforce_concurrency,
    root_usage_bytes,
)
from grok_worker.constants import (
    DEFAULT_CAP_BYTES,
    MANAGED_BY,
    MAX_CONCURRENT_WORKERS,
    SCHEMA_VERSION,
)
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import meta_path
from grok_worker.runner import RunConfig, run_worker
from grok_worker.session_process import SessionConfig


def test_canonical_capacity_defaults() -> None:
    """Shared cache 10 GiB, disposable cap 6 GiB, shared TTL 90 days."""
    assert DEFAULT_CACHE_MAX_BYTES == 10 * 1024**3
    assert DEFAULT_CAP_BYTES == 6 * 1024**3
    assert DEFAULT_CACHE_TTL_HOURS == 90 * 24
    assert DEFAULT_CACHE_TTL_HOURS == 2160


def test_session_capacity_defaults_derive_from_canonical() -> None:
    """Named-session defaults must not drift from one-shot/canonical constants."""
    fields = {f.name: f for f in SessionConfig.__dataclass_fields__.values()}
    assert fields["cap_bytes"].default is DEFAULT_CAP_BYTES
    assert fields["cache_max_bytes"].default is DEFAULT_CACHE_MAX_BYTES
    assert fields["cache_ttl_hours"].default is DEFAULT_CACHE_TTL_HOURS
    # RunConfig also shares the same canonical values.
    run_fields = {f.name: f for f in RunConfig.__dataclass_fields__.values()}
    assert run_fields["cap_bytes"].default is DEFAULT_CAP_BYTES
    assert run_fields["cache_max_bytes"].default is DEFAULT_CACHE_MAX_BYTES
    assert run_fields["cache_ttl_hours"].default is DEFAULT_CACHE_TTL_HOURS


def test_cap_refuse_when_over_after_gc(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
) -> None:
    big = tmp_roots["disposable"] / "blobdir"
    big.mkdir()
    (big / "fat.bin").write_bytes(b"x" * (1024 * 1024))
    now = utc_now()
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="blobdir",
        source_realpath=str(git_source),
        clone_realpath=str(big.resolve()),
        state=WorkerState.KEEP,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        keep_reason="fill-cap",
    )
    meta.write(meta_path(big))

    cfg = RunConfig(
        source=git_source,
        prompt="no room",
        backend="acp",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        cap_bytes=100_000,
        task_id="overcap",
    )
    with pytest.raises(CapacityError):
        run_worker(cfg)


def test_cap_allows_after_expired_cleanup(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
) -> None:
    expired = tmp_roots["disposable"] / "oldfail"
    expired.mkdir()
    (expired / "fat.bin").write_bytes(b"y" * (1024 * 1024))
    now = utc_now()
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="oldfail",
        source_realpath=str(git_source),
        clone_realpath=str(expired.resolve()),
        state=WorkerState.FAILED,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        retention_deadline=past,
    )
    meta.write(meta_path(expired))

    cfg = RunConfig(
        source=git_source,
        prompt="after cleanup",
        backend="acp",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        cap_bytes=500_000,
        task_id="aftergc",
    )
    outcome = run_worker(cfg)
    assert outcome.exit_code == 0
    assert not expired.exists()


def test_hidden_usage_counts_toward_cap(tmp_roots: dict[str, Path]) -> None:
    hidden = tmp_roots["disposable"] / ".hidden-big"
    hidden.mkdir()
    (hidden / "blob").write_bytes(b"z" * 50_000)
    usage = root_usage_bytes(tmp_roots["disposable"])
    assert usage >= 50_000


def test_exact_boundary_cap(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
) -> None:
    # usage == cap allowed; clone that would exceed fails after create rollback
    filler = tmp_roots["disposable"] / "fill"
    filler.mkdir()
    (filler / "a").write_bytes(b"x" * 10_000)
    now = utc_now()
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="fill",
        source_realpath=str(git_source),
        clone_realpath=str(filler.resolve()),
        state=WorkerState.KEEP,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        keep_reason="boundary",
    )
    meta.write(meta_path(filler))
    usage = root_usage_bytes(tmp_roots["disposable"])
    # Set cap to current usage — create would push over → CapacityError + rollback
    cfg = RunConfig(
        source=git_source,
        prompt="boundary",
        backend="acp",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        cap_bytes=usage,
        task_id="bound1",
    )
    with pytest.raises(CapacityError):
        run_worker(cfg)
    # Just-created clone must not remain
    assert not (tmp_roots["disposable"] / "grok-worker-bound1").exists()


def test_ten_active_reject(tmp_roots: dict[str, Path]) -> None:
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
            runner_pid=None,
            runner_start_token=None,
        )
        meta.write(meta_path(c))
    assert count_active_workers(tmp_roots["disposable"]) == 10
    with pytest.raises(ConcurrencyError):
        enforce_concurrency(tmp_roots["disposable"], 10)
