"""Lifecycle-managed named ACP sessions for one immutable logical task."""

from __future__ import annotations

import os
from pathlib import Path

from grok_worker.activity_lease import LeaseError, read_lease
from grok_worker.cache_policy import CachePolicy, ensure_cache_capacity
from grok_worker.capacity import enforce_cap, enforce_concurrency
from grok_worker.clone import create_workspace
from grok_worker.constants import MANAGED_BY, MAX_CONCURRENT_WORKERS, SCHEMA_VERSION
from grok_worker.deps import prepare_shared_env, worker_env_exports
from grok_worker.dispatcher import (
    DispatcherLease,
    make_run_id,
    reserve_dispatcher_capacity,
)
from grok_worker.finalize import finalize_run
from grok_worker.gc import gc_disposable_root
from grok_worker.grok_profile import scoped_worker_grok_home
from grok_worker.locks import root_lock, worker_lock
from grok_worker.metrics import read_task_metrics
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import meta_dir, meta_path
from grok_worker.prompt_cache import Role, TaskManifest, build_context_pack, build_prompt
from grok_worker.run_config import RunConfig, RunOutcome
from grok_worker.session_commands import build_close_cmd
from grok_worker.session_process import (
    SessionConfig,
    SessionOutcome,
    common_command,
    invoke,
    permission_contract_signature,
    prompt_turn,
)
from grok_worker.session_state import (
    FollowupContract,
    SessionContractError,
    SessionState,
    session_state_path,
    validate_followup,
)
from grok_worker.settings import agent_policy_environment
from grok_worker.task_id import validate_task_id


def _manifest(cfg: SessionConfig) -> TaskManifest:
    manifest = TaskManifest.from_file(cfg.manifest_file)
    validate_task_id(manifest.task_id)
    Role(cfg.role)
    if cfg.mode not in {"analysis", "implementation"}:
        raise SessionContractError("mode must be analysis or implementation")
    return manifest


def _acquire_session_invocation_lease(cfg: SessionConfig) -> DispatcherLease | None:
    """Transient capacity for one ACP invocation (not idle SESSION_OPEN)."""
    if not cfg.dispatcher_id:
        return None
    return reserve_dispatcher_capacity(
        cfg.shared_cache_root.resolve(),
        cfg.dispatcher_id,
        mode=cfg.mode,
        source_realpath=str(cfg.source.resolve()) if cfg.mode == "implementation" else None,
        limit=MAX_CONCURRENT_WORKERS,
    )


def start_session(cfg: SessionConfig) -> SessionOutcome:
    """Start a named session.

    With ``dispatcher_id``, the transient slot/source lease is acquired **before**
    clone creation, context-pack work, or dependency preparation so capacity is
    rejected without those side effects. The lease is held through ensure+prompt
    and released in ``finally`` on every path. Without ``dispatcher_id``, only
    root-scoped create-time concurrency applies (legacy).
    """
    manifest = _manifest(cfg)
    source = cfg.source.resolve()
    disposable = cfg.disposable_root.resolve()
    artifacts = cfg.artifact_root.resolve()
    cache = cfg.shared_cache_root.resolve()
    for root in (disposable, artifacts, cache):
        root.mkdir(parents=True, exist_ok=True)
    ensure_cache_capacity(CachePolicy(cache, cfg.cache_max_bytes, cfg.cache_ttl_hours))
    protected = [source, artifacts, cache, Path.home(), disposable]
    gc_disposable_root(disposable, protected=protected, shared_cache_root=cache)
    run_id = cfg.run_id or make_run_id()

    # Reject capacity before clone / context-pack / dependency preparation.
    lease: DispatcherLease | None = None
    try:
        if cfg.dispatcher_id:
            lease = _acquire_session_invocation_lease(cfg)
        with root_lock(disposable):
            # Create-time gates only for legacy root-scoped mode. Idle SESSION_OPEN
            # must not permanently reserve an OS slot; with dispatcher_id the
            # transient lease above is the capacity primitive.
            if not cfg.dispatcher_id:
                enforce_concurrency(disposable, MAX_CONCURRENT_WORKERS)
            enforce_cap(disposable, cfg.cap_bytes)
            clone, base, fingerprint, disclosure = create_workspace(
                source, disposable, manifest.task_id
            )
            now = dt_to_iso(utc_now()) or ""
            meta = WorkerMeta(
                schema_version=SCHEMA_VERSION,
                task_id=manifest.task_id,
                source_realpath=str(source),
                clone_realpath=str(clone),
                state=WorkerState.SESSION_OPEN,
                created_at=now,
                updated_at=now,
                managed_by=MANAGED_BY,
                base_commit=base,
                source_state_fingerprint=fingerprint,
                timeout_seconds=int(cfg.timeout) if cfg.timeout is not None else None,
                run_id=run_id,
                dispatcher_id=cfg.dispatcher_id,
                mode=cfg.mode,
                disclosure_summary=disclosure.to_dict(),
            )
            meta.write(meta_path(clone))
        pack = build_context_pack(source, base, cache)
        bundle = build_prompt(None, Role(cfg.role), pack, manifest)
        state = SessionState.new(
            task_id=manifest.task_id,
            source_realpath=str(source),
            clone_realpath=str(clone),
            base_sha=base,
            role=cfg.role,
            mode=cfg.mode,
            permission_signature=permission_contract_signature(cfg),
            session_name=f"grok-{manifest.task_id}",
            stable_prefix_hash=bundle.stable_prefix_hash,
            context_pack_hash=pack.context_pack_hash,
        )
        state.write(session_state_path(disposable, manifest.task_id))
        prompt = bundle.full_prompt
        if cfg.prepare_deps:
            prompt += "\n" + worker_env_exports(prepare_shared_env(clone, cache))
        prompt_turn(cfg, state, prompt, ensure=True)
        return SessionOutcome(manifest.task_id, state.status, state.prompt_count, str(clone))
    finally:
        if lease is not None:
            lease.release()


def followup_session(cfg: SessionConfig) -> SessionOutcome:
    """Prompt an open session; lease covers context-pack/prompt build and turn."""
    manifest = _manifest(cfg)
    path = session_state_path(cfg.disposable_root.resolve(), manifest.task_id)
    state = SessionState.read(path)
    validate_followup(
        state,
        FollowupContract(
            manifest.task_id,
            str(cfg.source.resolve()),
            state.base_sha,
            cfg.role,
            cfg.mode,
            permission_contract_signature(cfg),
        ),
    )
    lease: DispatcherLease | None = None
    try:
        lease = _acquire_session_invocation_lease(cfg)
        pack = build_context_pack(cfg.source.resolve(), state.base_sha, cfg.shared_cache_root)
        bundle = build_prompt(None, Role(cfg.role), pack, manifest)
        if bundle.stable_prefix_hash != state.stable_prefix_hash:
            raise SessionContractError("stable prefix changed; create a new session")
        prompt_turn(cfg, state, bundle.followup_prompt, ensure=False)
        return SessionOutcome(state.task_id, state.status, state.prompt_count, state.clone_realpath)
    finally:
        if lease is not None:
            lease.release()


def finalize_session(cfg: SessionConfig) -> SessionOutcome:
    """Close session and finalize artifacts while holding the invocation lease.

    Lease is acquired before session close and held through state update and
    ``finalize_run`` / artifact finalization. ``close_exit`` is initialized so a
    raising close/invoke cannot leave an UnboundLocalError on the error path.
    """
    manifest = _manifest(cfg)
    path = session_state_path(cfg.disposable_root.resolve(), manifest.task_id)
    state = SessionState.read(path)
    clone = Path(state.clone_realpath)
    log = cfg.artifact_root / f".run-log-{state.task_id}" / "agent.log"
    close_env = os.environ.copy()
    close_env.update(
        agent_policy_environment(
            model=cfg.model,
            reasoning_effort=cfg.reasoning_effort,
            allow_subagents=cfg.allow_subagents,
        )
    )
    close_env["GROK_WORKER_LIFECYCLE"] = "1"
    close_env["GROK_WORKER_TASK_ID"] = state.task_id
    close_env["GROK_WORKER_GROK_HOME"] = str(scoped_worker_grok_home(clone, close_env))

    lease: DispatcherLease | None = None
    # Bound before invoke so a raising close cannot UnboundLocalError later.
    close_exit = 1
    try:
        lease = _acquire_session_invocation_lease(cfg)
        with worker_lock(meta_dir(clone)):
            close_exit = invoke(
                build_close_cmd(common_command(cfg, clone), state.session_name),
                log,
                close_env,
                clone=clone,
                timeout=cfg.timeout,
                hard_timeout=cfg.hard_timeout,
            )
        state.session_closed = True
        state.status = "session_closed" if close_exit == 0 else "session_error"
        state.write(path)
        meta = WorkerMeta.read(meta_path(clone))
        run_cfg = RunConfig(
            source=cfg.source,
            prompt="",
            disposable_root=cfg.disposable_root,
            artifact_root=cfg.artifact_root,
            shared_cache_root=cfg.shared_cache_root,
            mode=cfg.mode,
            mcp_config=cfg.mcp_config,
            model=cfg.model,
            reasoning_effort=cfg.reasoning_effort,
            allow_subagents=cfg.allow_subagents,
            timeout=cfg.timeout,
            hard_timeout=cfg.hard_timeout,
            prepare_deps=False,
            task_id=state.task_id,
            dispatcher_id=cfg.dispatcher_id,
            run_id=cfg.run_id or meta.run_id,
        )
        audit: dict[str, object] = {
            "task_manifest": manifest.to_dict(),
            "session": {
                "name": state.session_name,
                "closed": state.session_closed,
                "promptCount": state.prompt_count,
                "stablePrefixHash": state.stable_prefix_hash,
                "contextPackHash": state.context_pack_hash,
            },
            "metrics": read_task_metrics(
                cfg.shared_cache_root / "metrics" / "worker-runs.jsonl", state.task_id
            ),
        }
        try:
            audit["activity_lease"] = read_lease(clone).to_dict()
        except LeaseError:
            audit["activity_lease"] = {"available": False}
        result: RunOutcome = finalize_run(
            run_cfg,
            clone,
            meta,
            cfg.disposable_root.resolve(),
            cfg.artifact_root.resolve(),
            [cfg.source.resolve(), cfg.artifact_root.resolve(), cfg.shared_cache_root.resolve()],
            log,
            close_exit,
            audit,
        )
        if result.clone_path is None:
            path.unlink(missing_ok=True)
        return SessionOutcome(
            result.task_id,
            result.state,
            state.prompt_count,
            result.clone_path,
            result.artifact_path,
        )
    finally:
        if lease is not None:
            lease.release()
