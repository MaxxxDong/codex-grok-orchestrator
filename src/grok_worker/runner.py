"""Outer lifecycle runner: capacity, clone, then worker execution."""

from __future__ import annotations

from pathlib import Path

from grok_worker.cache_policy import CachePolicy, ensure_cache_capacity
from grok_worker.capacity import (
    CapacityError,
    enforce_cap,
    enforce_concurrency,
    root_usage_bytes,
)
from grok_worker.clone import CloneError, create_workspace, make_task_id
from grok_worker.constants import MANAGED_BY, SCHEMA_VERSION
from grok_worker.gc import gc_disposable_root
from grok_worker.locks import root_lock
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import (
    default_artifact_root,
    default_disposable_root,
    default_shared_cache_root,
    meta_path,
)
from grok_worker.run_config import RunConfig, RunOutcome, default_agent_bin
from grok_worker.safety import SafetyError, safe_rmtree
from grok_worker.task_id import TaskIdError, validate_task_id
from grok_worker.worker_exec import execute_worker

__all__ = ["RunConfig", "RunOutcome", "run_worker"]


def run_worker(cfg: RunConfig) -> RunOutcome:
    source = cfg.source.resolve()
    if not source.exists():
        raise FileNotFoundError(f"source not found: {source}")

    disposable = (cfg.disposable_root or default_disposable_root(source)).resolve()
    artifacts = (cfg.artifact_root or default_artifact_root(disposable)).resolve()
    shared = (cfg.shared_cache_root or default_shared_cache_root()).resolve()
    disposable.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    shared.mkdir(parents=True, exist_ok=True)
    protected = [source, artifacts, shared, Path.home(), disposable]

    if not cfg.skip_pre_gc:
        gc_disposable_root(disposable, protected=protected, shared_cache_root=shared)
    ensure_cache_capacity(
        CachePolicy(
            root=shared,
            max_bytes=cfg.cache_max_bytes,
            ttl_hours=cfg.cache_ttl_hours,
        )
    )

    task_id = cfg.task_id or make_task_id()
    try:
        validate_task_id(task_id)
    except TaskIdError as exc:
        raise CloneError(str(exc)) from exc

    agent = cfg.agent_bin or default_agent_bin()
    clone: Path | None = None
    meta: WorkerMeta | None = None

    with root_lock(disposable):
        gc_disposable_root(
            disposable,
            protected=protected,
            clean_tmp=False,
            already_locked=True,
            shared_cache_root=shared,
        )
        enforce_concurrency(disposable, cfg.max_workers)
        enforce_cap(disposable, cfg.cap_bytes)
        clone, base, src_fp = create_workspace(
            source, disposable, task_id, include_dirty=cfg.include_dirty
        )
        usage = root_usage_bytes(disposable)
        if usage > cfg.cap_bytes:
            try:
                safe_rmtree(clone, disposable_root=disposable, protected=protected)
            except SafetyError:
                pass
            raise CapacityError(usage, cfg.cap_bytes, disposable)

        now = utc_now()
        meta = WorkerMeta(
            schema_version=SCHEMA_VERSION,
            task_id=task_id,
            source_realpath=str(source),
            clone_realpath=str(clone.resolve()),
            state=WorkerState.CREATING,
            created_at=dt_to_iso(now) or "",
            updated_at=dt_to_iso(now) or "",
            managed_by=MANAGED_BY,
            base_commit=base,
            source_state_fingerprint=src_fp,
            timeout_seconds=int(cfg.timeout) if cfg.timeout is not None else None,
        )
        meta.write(meta_path(clone))

    assert clone is not None and meta is not None
    return execute_worker(cfg, clone, meta, disposable, artifacts, shared, protected, agent)
