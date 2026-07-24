"""Execute a native or ACP worker under lock and drive finalization."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from grok_worker.activity_lease import (
    LeasedProcessResult,
    LeaseError,
    read_lease,
    run_with_activity_lease,
    terminate_process_tree,
)
from grok_worker.cache_fingerprint import (
    build_prompt_fingerprint,
    cache_ab_metrics_record,
    write_fingerprint_sidecar,
)
from grok_worker.cache_policy import cache_use_lease, shared_cache_environment
from grok_worker.completion_events import emit_completion_event
from grok_worker.constants import OUTPUT_DIR_NAME
from grok_worker.continuation import (
    ContinuationError,
    assert_continuation_usable,
    build_continuation_contract,
    clear_continuation,
    read_continuation,
    write_continuation,
)
from grok_worker.deps import DepsError, prepare_shared_env, worker_env_exports
from grok_worker.execution_contract import (
    ExecutionContract,
    observe_subagents_from_log,
)
from grok_worker.finalize import (
    classify_backend_failure,
    classify_live_backend_attention,
    finalize_run,
    mark_failed,
    try_collect,
)
from grok_worker.gc import gc_disposable_root
from grok_worker.grok_state import cleanup_clone_session_state, clone_session_root
from grok_worker.locks import worker_lock
from grok_worker.metrics import append_metric, extract_token_metrics_from_text, read_task_metrics
from grok_worker.models import WorkerMeta, WorkerState
from grok_worker.native_result import (
    NATIVE_RESULT_CAPTURE_GUIDANCE,
    parse_native_structured_result,
    persist_native_worker_result,
)
from grok_worker.paths import meta_dir, meta_path
from grok_worker.process_identity import (
    capture_identity,
    process_start_token,
    windows_descendant_pids,
)
from grok_worker.productive_progress import (
    capture_snapshot,
    evaluate_productive_progress,
    parse_model_turns_from_log,
)
from grok_worker.prompt_cache import OneShotModeError, build_one_shot_prompt
from grok_worker.result_schema import (
    ResultError,
    is_task_success,
    load_valid_result,
    result_path,
    write_captured_analysis_result,
)
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
from grok_worker.verification_runner import (
    VerificationRunnerError,
    capture_final_gate_evidence,
)


class Interrupt(Exception):
    """Raised when SIGINT/SIGTERM arrives during the worker run."""


def _budget_continuation_allowed(
    *,
    prior_continuations: int,
    before_fingerprint: str,
    after_fingerprint: str,
) -> bool:
    """Allow one recovery attempt, then require observable productive progress."""
    return prior_continuations == 0 or before_fingerprint != after_fingerprint


def _reap_process_tree(proc: subprocess.Popen[Any] | None) -> None:
    """Best-effort bounded cleanup for a backend process and its descendants."""
    if proc is None:
        return
    if os.name == "nt":
        targets = [proc.pid] if proc.poll() is None else []
        targets.extend(windows_descendant_pids(proc.pid))
        for pid in dict.fromkeys(targets):
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                    creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


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
        dynamic_extra_parts: list[str] = []
        execution = cfg.execution or ExecutionContract.empty()
        exec_payload = execution.to_worker_prompt_dict()
        if exec_payload:
            import json as _json

            dynamic_extra_parts.append(
                "--- GROK_EXECUTION_CONTRACT_V1 ---\n"
                + _json.dumps(exec_payload, indent=2, sort_keys=True, ensure_ascii=False)
                + "\n"
            )
            matrix = execution.expanded_final_matrix()
            if matrix:
                dynamic_extra_parts.append(
                    "Final verification matrix (do not narrow failed gates):\n"
                    + "\n".join(f"- {item}" for item in matrix)
                    + "\n"
                )
        use_native_schema = (
            cfg.backend == "native"
            and cfg.native_json_schema_result
            and oneshot_mode == "implementation"
            and not cfg.prompt_only
        )
        if use_native_schema:
            dynamic_extra_parts.append(NATIVE_RESULT_CAPTURE_GUIDANCE)
        dynamic_extra = "".join(dynamic_extra_parts) if dynamic_extra_parts else None
        try:
            prompt = build_one_shot_prompt(
                None, oneshot_mode, task_prompt, dynamic_suffix_extra=dynamic_extra
            )
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
        source_identity = meta.source_realpath or str(cfg.source or "prompt-only")
        fingerprint = build_prompt_fingerprint(prompt, source_realpath=source_identity)
        try:
            write_fingerprint_sidecar(clone, fingerprint)
        except OSError:
            pass

        continue_session = False
        if cfg.continue_task:
            if cfg.backend != "native":
                mark_failed(
                    meta,
                    clone,
                    retain_hours=cfg.failure_retain_hours,
                    message="native continuation requires backend=native",
                    shared_cache_root=shared,
                )
                art = try_collect(clone, meta, artifacts, disposable, agent_log)
                return RunOutcome(
                    task_id=task_id,
                    state=str(meta.state),
                    exit_code=1,
                    clone_path=str(clone),
                    artifact_path=str(art) if art else None,
                    message=meta.error_message or "continuation failed",
                    run_id=meta.run_id,
                    dispatcher_id=meta.dispatcher_id,
                )
            try:
                existing = read_continuation(clone)
                assert_continuation_usable(
                    existing,
                    task_id=task_id,
                    source_realpath=meta.source_realpath,
                    clone_realpath=str(clone.resolve()),
                    base_sha=meta.base_commit,
                    model=cfg.model,
                    reasoning_effort=cfg.reasoning_effort,
                    tool_policy=cfg.tool_policy(),
                    execution_signature=execution.signature(),
                    mode=oneshot_mode,
                )
                continue_session = True
            except ContinuationError as exc:
                mark_failed(
                    meta,
                    clone,
                    retain_hours=cfg.failure_retain_hours,
                    message=str(exc),
                    shared_cache_root=shared,
                )
                art = try_collect(clone, meta, artifacts, disposable, agent_log)
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

        def _record_child(process: subprocess.Popen[Any]) -> None:
            nonlocal child_proc
            child_proc = process
            meta.acpx_pid = child_proc.pid
            meta.acpx_start_token = process_start_token(child_proc.pid)
            meta.write(meta_path(clone))

        live_attention_reason: str | None = None
        live_output_tail = ""
        last_progress_eval = 0.0

        def _evaluate_live_progress() -> None:
            nonlocal last_progress_eval
            now_mono = time.monotonic()
            if now_mono - last_progress_eval < 15.0:
                return
            last_progress_eval = now_mono
            turns = parse_model_turns_from_log(live_output_tail)
            evaluate_productive_progress(
                clone,
                model_turns=turns,
                stall_turns=cfg.stall_turns,
                stall_seconds=cfg.stall_seconds,
                task_id=task_id,
                run_id=meta.run_id,
                dispatcher_id=meta.dispatcher_id,
                shared_cache_root=shared,
                emit_attention=True,
            )

        def _inspect_live_output(chunk: str) -> None:
            nonlocal live_attention_reason, live_output_tail
            live_output_tail = (live_output_tail + chunk)[-16_384:]
            if live_attention_reason is None:
                reason = classify_live_backend_attention(live_output_tail)
                if reason is not None:
                    live_attention_reason = reason
                    emit_completion_event(
                        task_id=task_id,
                        state=str(meta.state),
                        artifact_path=None,
                        shared_cache_root=shared,
                        run_id=meta.run_id,
                        dispatcher_id=meta.dispatcher_id,
                        kind="attention",
                        artifact_ready=False,
                        attention_required=True,
                        reason_code=reason,
                    )
            _evaluate_live_progress()

        automatic_continuations: list[str] = []
        continuation_break_reason: str | None = None
        final_backend_failure = None
        try:
            child_env = env
            if cfg.backend == "native":
                grok_bin = default_grok_bin()
                preflight_warning = check_grok_environment(grok_bin, cwd=clone, environ=child_env)
                if preflight_warning:
                    startup_warnings.append(preflight_warning)
                    with agent_log.open("a", encoding="utf-8") as stream:
                        stream.write(f"[grok-worker] warning: {preflight_warning}\n")

            # Monotonic wall covers every same-lifecycle budget continuation.
            process_started = time.monotonic()
            invocation = 0
            active_prompt_file = prompt_file
            while True:
                progress_before = capture_snapshot(clone).fingerprint()
                if cfg.backend == "native":
                    cmd = build_native_cmd(
                        cfg,
                        clone,
                        active_prompt_file,
                        continue_session=continue_session,
                    )
                else:
                    cmd = build_acpx_cmd(cfg, clone, agent, prompt)
                try:
                    segment_start = agent_log.stat().st_size
                except OSError:
                    segment_start = 0
                process_result = run_with_activity_lease(
                    cmd,
                    clone=clone,
                    log=agent_log,
                    env=child_env,
                    idle_timeout_seconds=cfg.timeout,
                    hard_timeout_seconds=cfg.hard_timeout,
                    initialize=invocation == 0,
                    on_start=_record_child,
                    on_output=_inspect_live_output,
                    on_tick=_evaluate_live_progress,
                )
                try:
                    with agent_log.open("rb") as segment_stream:
                        segment_stream.seek(segment_start)
                        segment = segment_stream.read().decode("utf-8", errors="replace")
                except OSError:
                    segment = ""
                final_backend_failure = (
                    classify_backend_failure(segment) if process_result.exit_code != 0 else None
                )
                if not (
                    cfg.backend == "native"
                    and process_result.timeout_message is None
                    and final_backend_failure is not None
                    and final_backend_failure.continuation_safe
                ):
                    break

                progress_after = capture_snapshot(clone).fingerprint()
                if not _budget_continuation_allowed(
                    prior_continuations=len(automatic_continuations),
                    before_fingerprint=progress_before,
                    after_fingerprint=progress_after,
                ):
                    continuation_break_reason = (
                        "repeated_budget_failure_without_productive_progress"
                    )
                    with agent_log.open("a", encoding="utf-8") as stream:
                        stream.write(
                            "[grok-worker] automatic continuation stopped: "
                            f"{continuation_break_reason}\n"
                        )
                    break

                automatic_continuations.append(final_backend_failure.kind)
                invocation += 1
                continue_session = True
                active_prompt_file = meta_dir(clone) / "prompt-budget-continuation.md"
                active_prompt_file.write_text(
                    "Continue the same logical task from the existing native session. "
                    "Preserve completed work, finish remaining implementation and "
                    "verification, then return the required structured result.\n",
                    encoding="utf-8",
                )
                with agent_log.open("a", encoding="utf-8") as stream:
                    stream.write(
                        "[grok-worker] automatic same-task continuation after "
                        f"{final_backend_failure.kind}\n"
                    )
            process_duration_seconds = time.monotonic() - process_started
        except PermissionError:
            # Lifecycle registration is an integrity boundary. Re-raise so the
            # finally block reaps an already-started Windows process tree.
            raise
        except (FileNotFoundError, OSError, ValueError) as exc:
            with agent_log.open("a", encoding="utf-8") as stream:
                stream.write(f"[grok-worker] startup failed: {exc}\n")
            process_result = LeasedProcessResult(127)
            process_duration_seconds = None
        worker_exit = process_result.exit_code
        child_proc = None
        meta.acpx_pid = None
        meta.acpx_start_token = None
        meta.acpx_exit_code = worker_exit
        meta.write(meta_path(clone))
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
        token_metrics = extract_token_metrics_from_text(log_text)
        backend_failure = (
            final_backend_failure
            if cfg.backend == "native" and worker_exit != 0
            else classify_backend_failure(log_text) if worker_exit != 0 else None
        )
        if backend_failure is not None:
            meta.failure_kind = backend_failure.kind
            meta.continuation_ready = backend_failure.continuation_safe and cfg.backend == "native"
            if not meta.error_message:
                meta.error_message = backend_failure.summary
        metrics_path = shared / "metrics" / "worker-runs.jsonl"
        requested_subagents = len(execution.subtasks)
        subagent_obs = observe_subagents_from_log(log_text, requested=requested_subagents)

        native_result_error: str | None = None
        verification_duration_seconds: float | None = None
        if use_native_schema and worker_exit == 0:
            verification_started = time.monotonic()
            try:
                native_result = parse_native_structured_result(log_text)
                remaining_hard = (
                    None
                    if cfg.hard_timeout is None or process_duration_seconds is None
                    else max(0.01, cfg.hard_timeout - process_duration_seconds)
                )
                native_result = capture_final_gate_evidence(
                    clone,
                    native_result,
                    execution.runner_final_gates(),
                    env=child_env,
                    timeout_seconds=cfg.timeout,
                    total_timeout_seconds=remaining_hard,
                )
                failed_gate = next(
                    (item for item in native_result.verification if item.exit_code != 0),
                    None,
                )
                if failed_gate is not None:
                    worker_exit = 1
                    meta.acpx_exit_code = worker_exit
                    meta.failure_kind = "runner_verification_failed"
                    meta.error_message = (
                        "runner-owned final gate failed with exit "
                        f"{failed_gate.exit_code}: {failed_gate.command}"
                    )
                persist_native_worker_result(clone, native_result, mode=oneshot_mode)
            except (ResultError, VerificationRunnerError, OSError, ValueError) as exc:
                native_result_error = str(exc)
                worker_exit = 1
                meta.acpx_exit_code = worker_exit
                meta.error_message = f"native structured result capture failed: {exc}"
            finally:
                verification_duration_seconds = time.monotonic() - verification_started

        total_duration_seconds = (
            time.monotonic() - process_started if process_duration_seconds is not None else None
        )
        metric_record: dict[str, object] = {
            "task_id": task_id,
            "mode": cfg.mode,
            "run_kind": "continuation" if continue_session else "one-shot",
            "backend": cfg.backend,
            "backend_process_exit_code": process_result.exit_code,
            "process_exit_code": worker_exit,
            "acpx_exit_code": worker_exit if cfg.backend == "acp" else None,
            "tool_signature": cfg.tool_policy().signature(),
            "continue_session": continue_session,
            "automatic_continuations": list(automatic_continuations),
            "continuation_break_reason": continuation_break_reason,
            "verification_duration_seconds": (
                round(verification_duration_seconds, 6)
                if verification_duration_seconds is not None
                else None
            ),
            "total_duration_seconds": (
                round(total_duration_seconds, 6)
                if total_duration_seconds is not None
                else None
            ),
            "subagents": subagent_obs.to_dict(),
        }
        metric_record.update(
            cache_ab_metrics_record(
                fingerprint=fingerprint,
                input_tokens=token_metrics.input_tokens,
                cached_tokens=token_metrics.cached_tokens,
                model_calls=token_metrics.model_calls,
                process_duration_seconds=(
                    round(process_duration_seconds, 6)
                    if process_duration_seconds is not None
                    else None
                ),
                cache_ratio=token_metrics.cache_ratio,
                cache_ratio_basis=token_metrics.cache_ratio_basis,
            )
        )
        append_metric(metrics_path, metric_record, token_metrics)

        continuation_result_ok = False
        if cfg.write_continuation and worker_exit == 0 and native_result_error is None:
            try:
                if oneshot_mode in {"analysis", "research"} and not result_path(clone).exists():
                    write_captured_analysis_result(clone, agent_log)
                continuation_result = load_valid_result(clone)
                continuation_result_ok = is_task_success(
                    worker_exit,
                    continuation_result,
                    mode="analysis" if oneshot_mode in {"analysis", "research"} else oneshot_mode,
                )
            except (ResultError, OSError, ValueError):
                continuation_result_ok = False

        # Persist explicit successful continuations and recoverable native budget
        # interruptions. The latter remain FAILED, but can resume the same clone.
        preserve_session = False
        if (
            cfg.backend == "native"
            and (
                (continuation_result_ok and cfg.keep_reason and cfg.write_continuation)
                or meta.continuation_ready
            )
        ):
            try:
                cont = build_continuation_contract(
                    task_id=task_id,
                    source_realpath=meta.source_realpath,
                    clone_realpath=str(clone.resolve()),
                    base_sha=meta.base_commit or "",
                    model=cfg.model,
                    reasoning_effort=cfg.reasoning_effort,
                    tool_policy=cfg.tool_policy(),
                    execution_signature=execution.signature(),
                    mode=oneshot_mode,
                    run_id=meta.run_id,
                )
                write_continuation(clone, cont)
                preserve_session = True
            except (ContinuationError, OSError, ValueError) as exc:
                startup_warnings.append(f"continuation write skipped: {exc}")
        elif cfg.backend == "native":
            try:
                clear_continuation(clone)
            except ContinuationError:
                pass
            if cfg.write_continuation:
                # A requested bounded continuation that did not become usable is
                # a normal failed run with 24h evidence retention, never KEEP.
                cfg.keep_reason = None

        audit: dict[str, object] = {
            "metrics": read_task_metrics(metrics_path, task_id),
            "backend": cfg.backend,
            "startup_warnings": startup_warnings,
            "prompt_fingerprint": fingerprint.to_dict(),
            "tool_policy": cfg.tool_policy().to_dict(),
            "subagents": subagent_obs.to_dict(),
            "execution_contract": execution.to_dict(),
            "native_json_schema_result": use_native_schema,
            "automatic_continuations": list(automatic_continuations),
            "continuation_break_reason": continuation_break_reason,
            "preserve_native_session": preserve_session,
            "session": {
                "name": None,
                "closed": not preserve_session,
                "retained": preserve_session,
                "mode": "native-continuation" if preserve_session else "one-shot",
            },
        }
        if native_result_error:
            audit["native_result_error"] = native_result_error
        try:
            audit["activity_lease"] = read_lease(clone).to_dict()
        except LeaseError:
            audit["activity_lease"] = {"available": False}
        outcome = finalize_run(
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
        # Stash for finally-block session cleanup decision.
        env["GROK_WORKER_PRESERVE_SESSION"] = "1" if preserve_session else "0"
        return outcome
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
        _reap_process_tree(child_proc)
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        wlock.release()
        cache_lease.release()
        session_root = clone_session_root(clone)
        preserve_session = False
        try:
            cont_path = meta_dir(clone) / "continuation.json"
            preserve_session = (
                cfg.backend == "native"
                and cont_path.is_file()
                and not cont_path.is_symlink()
                and (
                    (cfg.write_continuation and cfg.keep_reason is not None)
                    or meta.continuation_ready
                )
            )
        except OSError:
            preserve_session = False
        if not preserve_session:
            try:
                cleanup_clone_session_state(clone)
            except (OSError, SafetyError, ValueError) as exc:
                print(
                    f"[grok-worker] warning: Grok clone session cleanup skipped: {exc}",
                    file=sys.stderr,
                )
            try:
                clear_continuation(clone)
            except ContinuationError:
                pass
        session_cleaned = not session_root.exists() and not session_root.is_symlink()
        session_retained = preserve_session
        if not cfg.skip_post_gc:
            try:
                gc_protected = list(protected)
                if meta.state == WorkerState.SUCCESS and not meta.terminal_event_ready:
                    gc_protected.append(clone)
                gc_disposable_root(
                    disposable,
                    protected=gc_protected,
                    shared_cache_root=shared,
                )
            except (OSError, ValueError) as exc:
                print(f"[grok-worker] warning: post-run GC skipped: {exc}", file=sys.stderr)

        clone_cleaned = not clone.exists() and not clone.is_symlink()
        artifact_ready = bool(
            meta.artifact_complete and meta.artifact_path and Path(meta.artifact_path).is_dir()
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
                or (not session_cleaned and not session_retained)
                or (meta.state == WorkerState.SUCCESS and not clone_cleaned)
            )
            reason_code = None
            if meta.state == WorkerState.FAILED:
                reason_code = meta.failure_kind or "terminal_failed"
            elif not session_cleaned and not session_retained:
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
                session_retained=session_retained,
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
                session_retained=session_retained,
                attention_required=True,
                reason_code="unhandled_worker_exception",
            )
