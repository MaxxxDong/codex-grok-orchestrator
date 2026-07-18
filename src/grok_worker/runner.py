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
from grok_worker.clone import (
    CloneError,
    clone_path_for,
    create_prompt_only_workspace,
    create_workspace,
    make_task_id,
)
from grok_worker.constants import MANAGED_BY, PROMPT_ONLY_SOURCE, SCHEMA_VERSION
from grok_worker.dispatcher import (
    DispatcherLease,
    make_run_id,
    reserve_dispatcher_capacity,
)
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
    if cfg.prompt_only:
        if cfg.mode == "implementation":
            raise CloneError("prompt-only mode rejects implementation mode")
        if cfg.include_dirty or cfg.include_dirty_paths:
            raise CloneError("prompt-only mode rejects dirty/source flags")
        if cfg.source is not None:
            raise CloneError(
                "prompt-only must not be combined with a non-null source "
                "(library/API path rejects silently-ignored source)"
            )
        source: Path | None = None
        source_realpath = PROMPT_ONLY_SOURCE
    else:
        if cfg.source is None:
            raise FileNotFoundError("source is required unless --prompt-only")
        source = cfg.source.resolve()
        if not source.exists():
            raise FileNotFoundError(f"source not found: {source}")
        source_realpath = str(source)

    if source is not None:
        disposable = (cfg.disposable_root or default_disposable_root(source)).resolve()
    else:
        disposable = (
            cfg.disposable_root or (Path.cwd() / ".grok-disposable")
        ).resolve()
    artifacts = (cfg.artifact_root or default_artifact_root(disposable)).resolve()
    shared = (cfg.shared_cache_root or default_shared_cache_root()).resolve()
    disposable.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    shared.mkdir(parents=True, exist_ok=True)
    protected = [artifacts, shared, Path.home(), disposable]
    if source is not None:
        protected.insert(0, source)

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

    run_id = cfg.run_id or make_run_id()
    agent = (cfg.agent_bin or default_agent_bin()) if cfg.backend == "acp" else ""
    clone: Path | None = None
    meta: WorkerMeta | None = None
    mode = "analysis" if cfg.prompt_only else cfg.mode
    lease: DispatcherLease | None = None

    try:
        with root_lock(disposable):
            gc_disposable_root(
                disposable,
                protected=protected,
                clean_tmp=False,
                already_locked=True,
                shared_cache_root=shared,
            )
            # Atomic capacity reservation before clone/deps.
            if cfg.dispatcher_id:
                lease = reserve_dispatcher_capacity(
                    shared,
                    cfg.dispatcher_id,
                    mode=mode,
                    # Workers edit independent disposable clones. Integration
                    # remains Root-owned, so same-source startup exclusion only
                    # serialized safe work and reduced throughput.
                    source_realpath=None,
                    limit=cfg.max_workers,
                )
            else:
                # Backward compatible: root-scoped only (no silent cross-root claim).
                enforce_concurrency(disposable, cfg.max_workers)
            enforce_cap(disposable, cfg.cap_bytes)

            # A retained failed clone must not prevent a deliberate retry. Keep
            # the old evidence and allocate a fresh, returned task id.
            requested_task_id = task_id
            while clone_path_for(disposable, task_id).exists() or clone_path_for(
                disposable, task_id
            ).is_symlink():
                stem = requested_task_id[:55].rstrip("._-") or "task"
                task_id = f"{stem}-{make_task_id()[:6]}"
                validate_task_id(task_id)

            if cfg.prompt_only:
                clone, base, src_fp, disclosure = create_prompt_only_workspace(
                    disposable, task_id
                )
            else:
                assert source is not None
                clone, base, src_fp, disclosure = create_workspace(
                    source,
                    disposable,
                    task_id,
                    include_dirty=cfg.include_dirty,
                    dirty_allowlist=cfg.include_dirty_paths or None,
                )
            usage = root_usage_bytes(disposable)
            if usage > cfg.cap_bytes:
                try:
                    safe_rmtree(clone, disposable_root=disposable, protected=protected)
                except SafetyError:
                    pass
                raise CapacityError(usage, cfg.cap_bytes, disposable)

            now = utc_now()
            disc_dict = disclosure.to_dict()
            meta = WorkerMeta(
                schema_version=SCHEMA_VERSION,
                task_id=task_id,
                source_realpath=source_realpath,
                clone_realpath=str(clone.resolve()),
                state=WorkerState.CREATING,
                created_at=dt_to_iso(now) or "",
                updated_at=dt_to_iso(now) or "",
                managed_by=MANAGED_BY,
                base_commit=base,
                source_state_fingerprint=src_fp,
                timeout_seconds=int(cfg.timeout) if cfg.timeout is not None else None,
                run_id=run_id,
                dispatcher_id=cfg.dispatcher_id,
                mode=mode,
                backend=cfg.backend,
                disclosure_summary=disc_dict,
            )
            meta.write(meta_path(clone))

        assert clone is not None and meta is not None
        # Prompt-only: never prepare deps against a source tree.
        if cfg.prompt_only:
            cfg.prepare_deps = False
        return execute_worker(
            cfg, clone, meta, disposable, artifacts, shared, protected, agent
        )
    finally:
        if lease is not None:
            lease.release()
