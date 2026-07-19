"""Execute a native or ACP worker under lock and drive finalization."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from grok_worker.activity_lease import (
    LeasedProcessResult,
    LeaseError,
    read_lease,
    run_with_activity_lease,
    terminate_process_tree,
)
from grok_worker.cache_policy import cache_use_lease, shared_cache_environment
from grok_worker.completion_events import emit_completion_event
from grok_worker.constants import OUTPUT_DIR_NAME
from grok_worker.deps import DepsError, prepare_shared_env, worker_env_exports
from grok_worker.finalize import finalize_run, mark_failed, try_collect
from grok_worker.gc import gc_disposable_root
from grok_worker.grok_state import cleanup_clone_session_state, clone_session_root
from grok_worker.locks import worker_lock
from grok_worker.metrics import append_metric, extract_token_metrics_from_text, read_task_metrics
from grok_worker.models import WorkerMeta, WorkerState
from grok_worker.paths import meta_dir, meta_path
from grok_worker.process_identity import capture_identity, process_start_token
from grok_worker.prompt_cache import OneShotModeError, build_one_shot_prompt
from grok_worker.run_config import (
    RunConfig,
    RunOutcome,
    build_acpx_cmd,
    build_native_cmd,
    check_grok_environment,
    default_grok_bin,
)
from grok_worker.safety import SafetyError
from grok_worker.settings import agent_policy_environment


class Interrupt(Exception):
    """Raised when SIGINT/SIGTERM arrives during the worker run."""


_REASONING_DOWNGRADE_WARNING = "model does not support reasoning effort; ignoring"


def execute_worker(
    cfg: RunConfig,
    clone: Path,
    meta: WorkerMeta,
    disposable: Path,
    artifacts: Path,
    shared: Path,
    protected: list[Path],
    agent: str,
) -> RunOutcome:
    task_id = meta.task_id
    wlock = worker_lock(meta_dir(clone))
    agent_log: Path | None = None
    child_proc: subprocess.Popen[Any] | None = None
    cache_lease = cache_use_lease(shared)

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal child_proc
        if child_proc is not None and child_proc.poll() is None:
            terminate_process_tree(child_proc)
        raise Interrupt(f"received signal {signum}")

    prev_int = signal.signal(signal.SIGINT, _handle_signal)
    prev_term = signal.signal(signal.SIGTERM, _handle_signal)

    try:
        wlock.acquire()
        cache_lease.acquire()
        dep_env = shared_cache_environment(shared)
        startup_warnings: list[str] = []
        if cfg.prepare_deps:
            try:
                dep_env.update(prepare_shared_env(clone, shared))
            except (DepsError, OSError) as exc:
                startup_warnings.append(f"dependency prewarm skipped: {exc}")
        if cfg.backend == "native":
            # Grok's workspace sandbox cannot write the host-level shared cache.
            # Keep prepared environments shared/read-only, but place mutable tool
            # caches inside the disposable clone so verification starts cleanly.
            runtime_cache = clone / OUTPUT_DIR_NAME / ".runtime-cache"
            runtime_cache.mkdir(parents=True, mode=0o700, exist_ok=True)
            dep_env.update(
                {
                    "UV_CACHE_DIR": str(runtime_cache / "uv"),
                    "PIP_CACHE_DIR": str(runtime_cache / "pip"),
                    "NPM_CONFIG_CACHE": str(runtime_cache / "npm"),
                    "POETRY_CACHE_DIR": str(runtime_cache / "poetry"),
                }
            )
        oneshot_mode = "research" if cfg.prompt_only else cfg.mode
        task_prompt = cfg.prompt
        if dep_env:
            # Keep the Skill-owned prefix byte-stable across clones and tasks.
            # Runtime paths live in the child environment, never at prompt byte 0.
            task_prompt = worker_env_exports(dep_env) + "\n" + task_prompt
        try:
            prompt = build_one_shot_prompt(None, oneshot_mode, task_prompt)
        except OneShotModeError as exc:
            mark_failed(
                meta,
                clone,
                retain_hours=cfg.failure_retain_hours,
                message=str(exc),
                shared_cache_root=shared,
            )
            art = try_collect(clone, meta, artifacts, disposable, None)
            if art:
                meta.artifact_path = str(art)
                meta.write(meta_path(clone))
            return RunOutcome(
                task_id=task_id,
                state=str(meta.state),
                exit_code=1,
                clone_path=str(clone),
                artifact_path=str(art) if art else None,
                message=str(exc),
                run_id=meta.run_id,
                dispatcher_id=meta.dispatcher_id,
            )
        rpid, rtok = capture_identity()
        meta.state = WorkerState.RUNNING
        meta.runner_pid = rpid
        meta.runner_start_token = rtok
        meta.pid = rpid
        meta.touch()
        meta.write(meta_path(clone))

        log_dir = artifacts / f".run-log-{task_id}"
        log_dir.mkdir(parents=True, exist_ok=True)
        agent_log = log_dir / "agent.log"
        env = os.environ.copy()
        env.update(dep_env)
        env.update(
            agent_policy_environment(
                model=cfg.model,
                reasoning_effort=cfg.reasoning_effort,
                allow_subagents=cfg.allow_subagents,
            )
        )
        env["GROK_WORKER_LIFECYCLE"] = "1"
        env["GROK_WORKER_TASK_ID"] = task_id

        warning_text = "".join(f"[grok-worker] warning: {item}\n" for item in startup_warnings)
        agent_log.write_text(warning_text, encoding="utf-8")
        prompt_file = meta_dir(clone) / "prompt-one-shot.md"
        prompt_file.write_text(prompt, encoding="utf-8")

        def _record_child(process: subprocess.Popen[Any]) -> None:
            nonlocal child_proc
            child_proc = process
            meta.acpx_pid = child_proc.pid
            meta.acpx_start_token = process_start_token(child_proc.pid)
            meta.write(meta_path(clone))

        try:
            child_env = env
            if cfg.backend == "native":
                grok_bin = default_grok_bin()
                preflight_warning = check_grok_environment(
                    grok_bin, cwd=clone, environ=child_env
                )
                if preflight_warning:
                    startup_warnings.append(preflight_warning)
                    with agent_log.open("a", encoding="utf-8") as stream:
                        stream.write(f"[grok-worker] warning: {preflight_warning}\n")
                cmd = build_native_cmd(cfg, clone, prompt_file)
            else:
                cmd = build_acpx_cmd(cfg, clone, agent, prompt)
            process_result = run_with_activity_lease(
                cmd,
                clone=clone,
                log=agent_log,
                env=child_env,
                idle_timeout_seconds=cfg.timeout,
                hard_timeout_seconds=cfg.hard_timeout,
                on_start=_record_child,
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            with agent_log.open("a", encoding="utf-8") as stream:
                stream.write(f"[grok-worker] startup failed: {exc}\n")
            process_result = LeasedProcessResult(127)
        worker_exit = process_result.exit_code
        child_proc = None
        meta.acpx_exit_code = worker_exit
        if process_result.timeout_message:
            meta.error_message = process_result.timeout_message
        try:
            log_text = agent_log.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            log_text = ""
        if (
            cfg.backend == "native"
            and cfg.reasoning_effort
            and _REASONING_DOWNGRADE_WARNING in log_text
        ):
            worker_exit = 78
            meta.acpx_exit_code = worker_exit
            meta.error_message = (
                f"native Grok ignored requested reasoning effort {cfg.reasoning_effort!r}"
            )
        metrics_path = shared / "metrics" / "worker-runs.jsonl"
        append_metric(
            metrics_path,
            {
                "task_id": task_id,
                "mode": cfg.mode,
                "run_kind": "one-shot",
                "backend": cfg.backend,
                "process_exit_code": worker_exit,
                "acpx_exit_code": worker_exit if cfg.backend == "acp" else None,
            },
            extract_token_metrics_from_text(log_text),
        )
        audit: dict[str, object] = {
            "metrics": read_task_metrics(metrics_path, task_id),
            "backend": cfg.backend,
            "startup_warnings": startup_warnings,
        }
        try:
            audit["activity_lease"] = read_lease(clone).to_dict()
        except LeaseError:
            audit["activity_lease"] = {"available": False}
        return finalize_run(
            cfg,
            clone,
            meta,
            disposable,
            artifacts,
            protected,
            agent_log,
            worker_exit,
            audit,
        )
    except Interrupt as exc:
        mark_failed(
            meta,
            clone,
            retain_hours=cfg.failure_retain_hours,
            message=str(exc),
            exit_code=130,
            interrupted=True,
            shared_cache_root=shared,
        )
        art = try_collect(clone, meta, artifacts, disposable, agent_log)
        return RunOutcome(
            task_id=task_id,
            state=str(meta.state),
            exit_code=130,
            clone_path=str(clone),
            artifact_path=str(art) if art else None,
            message=str(exc),
            run_id=meta.run_id,
            dispatcher_id=meta.dispatcher_id,
        )
    finally:
        unhandled_exception = sys.exc_info()[0] is not None
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        wlock.release()
        cache_lease.release()
        session_root = clone_session_root(clone)
        try:
            cleanup_clone_session_state(clone)
        except (OSError, SafetyError, ValueError) as exc:
            print(
                f"[grok-worker] warning: Grok clone session cleanup skipped: {exc}",
                file=sys.stderr,
            )
        session_cleaned = not session_root.exists() and not session_root.is_symlink()
        if not cfg.skip_post_gc:
            try:
                gc_disposable_root(
                    disposable, protected=protected, shared_cache_root=shared
                )
            except (OSError, ValueError) as exc:
                print(f"[grok-worker] warning: post-run GC skipped: {exc}", file=sys.stderr)

        clone_cleaned = not clone.exists() and not clone.is_symlink()
        artifact_ready = bool(
            meta.artifact_complete
            and meta.artifact_path
            and Path(meta.artifact_path).is_dir()
        )
        terminal_states = {
            WorkerState.SUCCESS,
            WorkerState.FAILED,
            WorkerState.KEEP,
            WorkerState.LEGACY_IMPORTED,
        }
        if meta.state in terminal_states:
            attention_required = (
                meta.state == WorkerState.FAILED
                or not session_cleaned
                or (meta.state == WorkerState.SUCCESS and not clone_cleaned)
            )
            reason_code = None
            if meta.state == WorkerState.FAILED:
                reason_code = "terminal_failed"
            elif not session_cleaned:
                reason_code = "session_cleanup_incomplete"
            elif meta.state == WorkerState.SUCCESS and not clone_cleaned:
                reason_code = "clone_cleanup_incomplete"
            emit_completion_event(
                task_id=meta.task_id,
                state=str(meta.state),
                artifact_path=meta.artifact_path,
                shared_cache_root=shared,
                run_id=meta.run_id,
                dispatcher_id=meta.dispatcher_id,
                kind="settled",
                exit_code=meta.exit_code,
                artifact_ready=artifact_ready,
                clone_cleaned=clone_cleaned,
                session_cleaned=session_cleaned,
                attention_required=attention_required,
                reason_code=reason_code,
            )
        elif unhandled_exception:
            emit_completion_event(
                task_id=meta.task_id,
                state="worker_crashed",
                artifact_path=meta.artifact_path,
                shared_cache_root=shared,
                run_id=meta.run_id,
                dispatcher_id=meta.dispatcher_id,
                kind="attention",
                exit_code=meta.exit_code,
                artifact_ready=artifact_ready,
                clone_cleaned=clone_cleaned,
                session_cleaned=session_cleaned,
                attention_required=True,
                reason_code="unhandled_worker_exception",
            )
