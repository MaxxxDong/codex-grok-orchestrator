"""CLI command implementations for the Typer app."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer

from grok_worker.activity_lease import LeaseError, read_lease, set_lease_policy
from grok_worker.cache_policy import (
    DEFAULT_CACHE_MAX_BYTES,
    DEFAULT_CACHE_TTL_HOURS,
    CacheCapacityError,
    CachePolicy,
    gc_shared_cache,
)
from grok_worker.capacity import CapacityError, ConcurrencyError
from grok_worker.completion_events import (
    DEFAULT_EVENT_WAIT_SECONDS,
    EventWaitError,
    emit_completion_event,
    events_to_payload,
    list_completion_events,
)
from grok_worker.config_apply import ConfigApplyError, apply_config
from grok_worker.constants import (
    DEFAULT_ACPX_TIMEOUT,
    DEFAULT_CAP_BYTES,
    DEFAULT_FAILURE_RETAIN_HOURS,
    DEFAULT_HARD_TIMEOUT,
    DEFAULT_WATCH_WAIT_SECONDS,
    MAX_CONCURRENT_WORKERS,
    MAX_EVENT_WAIT_SECONDS,
)
from grok_worker.detached import (
    DetachedStartError,
    run_config_from_payload,
    start_detached_run,
)
from grok_worker.disclosure import DisclosureError, disclosure_preflight
from grok_worker.dispatcher import (
    DispatcherConcurrencyError,
    SameSourceConflictError,
    make_run_id,
)
from grok_worker.gc import gc_disposable_root, is_active
from grok_worker.health import collect_health
from grok_worker.legacy import LegacyClass, LegacyError, import_legacy, list_unmarked
from grok_worker.models import WorkerMeta
from grok_worker.paths import (
    default_artifact_root,
    default_disposable_root,
    default_shared_cache_root,
    is_managed_clone,
    meta_path,
)
from grok_worker.run_config import default_one_shot_backend
from grok_worker.runner import RunConfig, run_worker
from grok_worker.settings import default_mcp_config, default_model, default_reasoning_effort
from grok_worker.status import collect_status, format_status_json, format_status_text
from grok_worker.watch import watch_workers


def _shared(shared_cache_root: Path | None) -> Path:
    if shared_cache_root is not None:
        return Path(shared_cache_root).resolve()
    return default_shared_cache_root()


def _resolve_disposable(disposable_root: Path | None, source: Path | None) -> Path:
    if disposable_root is not None:
        return Path(disposable_root).resolve()
    if source is not None:
        return default_disposable_root(Path(source)).resolve()
    return default_disposable_root(Path.cwd()).resolve()


def _startup_reason_code(exc: BaseException) -> str:
    name = type(exc).__name__
    chars: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            chars.append("_")
        chars.append(char.lower())
    return f"startup_{''.join(chars)}"


def _emit_startup_attention(cfg: RunConfig, exc: BaseException, exit_code: int) -> None:
    emit_completion_event(
        task_id=cfg.task_id or cfg.run_id or "startup-failed",
        state="startup_failed",
        artifact_path=None,
        shared_cache_root=cfg.shared_cache_root,
        run_id=cfg.run_id,
        dispatcher_id=cfg.dispatcher_id,
        kind="attention",
        exit_code=exit_code,
        artifact_ready=False,
        clone_cleaned=True,
        session_cleaned=True,
        attention_required=True,
        reason_code=_startup_reason_code(exc),
    )


def _find_disclosure_error(exc: BaseException) -> DisclosureError | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, DisclosureError):
            return current
        current = current.__cause__ or current.__context__
    return None


def _execute_run_config(cfg: RunConfig) -> None:
    try:
        outcome = run_worker(cfg)
    except (
        CapacityError,
        ConcurrencyError,
        CacheCapacityError,
        DispatcherConcurrencyError,
        SameSourceConflictError,
    ) as exc:
        _emit_startup_attention(cfg, exc, 2)
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    except Exception as exc:  # noqa: BLE001
        _emit_startup_attention(cfg, exc, 1)
        disclosure = _find_disclosure_error(exc)
        if disclosure is not None and disclosure.blocked_items:
            typer.echo("disclosure blocked paths (values omitted):", err=True)
            for item in disclosure.blocked_items:
                typer.echo(
                    f"  {item['path']} reason={item['reason_code']}",
                    err=True,
                )
        typer.echo(f"run failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps(
            {
                "task_id": outcome.task_id,
                "run_id": outcome.run_id,
                "dispatcher_id": outcome.dispatcher_id,
                "state": outcome.state,
                "exit_code": outcome.exit_code,
                "clone_path": outcome.clone_path,
                "artifact_path": outcome.artifact_path,
                "message": outcome.message,
            },
            indent=2,
        )
    )
    raise typer.Exit(int(outcome.exit_code))


def cmd_run(
    source: Path | None = typer.Option(
        None, "--source", help="Source repository or tree (omit with --prompt-only)"
    ),
    prompt: str | None = typer.Option(None, "--prompt"),
    prompt_file: Path | None = typer.Option(None, "--prompt-file"),
    disposable_root: Path | None = typer.Option(None, "--disposable-root"),
    artifact_root: Path | None = typer.Option(None, "--artifact-root"),
    shared_cache_root: Path | None = typer.Option(None, "--shared-cache-root"),
    keep: str | None = typer.Option(None, "--keep", help="Nonempty reason to retain"),
    mode: str = typer.Option("implementation", "--mode"),
    timeout: int = typer.Option(
        DEFAULT_ACPX_TIMEOUT,
        "--timeout",
        help=f"Inactivity lease seconds (default {DEFAULT_ACPX_TIMEOUT}); real activity renews it",
    ),
    hard_timeout: int = typer.Option(
        DEFAULT_HARD_TIMEOUT,
        "--hard-timeout",
        help=f"Absolute safety cap seconds (default {DEFAULT_HARD_TIMEOUT}; 0 disables)",
    ),
    task_id: str | None = typer.Option(None, "--task-id"),
    run_id: str | None = typer.Option(None, "--run-id", help="Unique run id (auto if omitted)"),
    dispatcher_id: str | None = typer.Option(
        None,
        "--dispatcher-id",
        help="Per-dispatcher concurrency scope (also GROK_WORKER_DISPATCHER_ID). "
        "Required for cross-root max-10 enforcement; without it only root-scoped limits apply.",
    ),
    backend: str = typer.Option(
        default_one_shot_backend(),
        "--backend",
        help="One-shot backend: native (default) or ACP compatibility path",
    ),
    acpx_bin: str | None = typer.Option(
        None,
        "--acpx-bin",
        help="Explicit acpx override (default: pinned grok-worker runtime on Windows)",
    ),
    agent_bin: str | None = typer.Option(None, "--agent-bin"),
    mcp_config: str | None = typer.Option(None, "--mcp-config"),
    model: str | None = typer.Option(None, "--model"),
    reasoning_effort: str | None = typer.Option(None, "--reasoning-effort"),
    allow_subagents: bool = typer.Option(True, "--allow-subagents/--no-subagents"),
    disable_web_search: bool = typer.Option(
        False,
        "--disable-web-search",
        help="Opt-in pure-code: pass --disable-web-search to native Grok",
    ),
    disallowed_tool: list[str] | None = typer.Option(
        None,
        "--disallowed-tool",
        help="Built-in tool name to deny (repeatable); mapped to native --disallowed-tools",
    ),
    continue_task: bool = typer.Option(
        False,
        "--continue",
        help="Native same-task continuation for a kept clone with valid continuation.json",
    ),
    write_continuation: bool = typer.Option(
        False,
        "--write-continuation",
        help="After a successful --keep native run, store continuation metadata for --continue",
    ),
    execution_manifest: Path | None = typer.Option(
        None,
        "--execution-manifest",
        help="Optional JSON file with targetFiles/focusedChecks/finalGates/riskTags/subtasks",
    ),
    stall_turns: int = typer.Option(
        8,
        "--stall-turns",
        help="Attention after this many model turns without productive progress",
    ),
    stall_seconds: float = typer.Option(
        900.0,
        "--stall-seconds",
        help="Attention after this many seconds without productive progress",
    ),
    no_native_json_schema: bool = typer.Option(
        False,
        "--no-native-json-schema",
        help="Disable native JSON Schema final-result capture (ACP-like disk result.json)",
    ),
    failure_retain_hours: int = typer.Option(
        DEFAULT_FAILURE_RETAIN_HOURS, "--failure-retain-hours"
    ),
    max_workers: int = typer.Option(
        MAX_CONCURRENT_WORKERS,
        "--max-workers",
        envvar="GROK_WORKER_MAX_WORKERS",
        min=1,
        help="Maximum active workers admitted under this disposable root",
    ),
    cap_bytes: int = typer.Option(DEFAULT_CAP_BYTES, "--cap-bytes"),
    no_prepare_deps: bool = typer.Option(False, "--no-prepare-deps"),
    include_dirty: bool = typer.Option(
        False,
        "--include-dirty",
        help="Deprecated compatibility flag; safe dirty files are snapshotted automatically",
    ),
    include_dirty_path: list[str] | None = typer.Option(
        None,
        "--include-dirty-path",
        help="Repository-relative dirty path allowlist (repeatable). "
        "Renames may require both old and new paths.",
    ),
    prompt_only: bool = typer.Option(
        False,
        "--prompt-only",
        help="Prompt-only research/analysis: no source tree; empty managed workspace",
    ),
    cache_max_bytes: int = typer.Option(DEFAULT_CACHE_MAX_BYTES, "--cache-max-bytes"),
    cache_ttl_hours: float = typer.Option(DEFAULT_CACHE_TTL_HOURS, "--cache-ttl-hours"),
    detach: bool = typer.Option(
        False,
        "--detach",
        help="Return after launch; observe with grok-worker watch instead of terminal polling",
    ),
) -> None:
    """Create a disposable clone, run Grok, collect artifacts, and finalize."""
    if backend not in {"native", "acp"}:
        typer.echo("backend must be native or acp", err=True)
        raise typer.Exit(2)
    if prompt_only:
        if mode == "implementation":
            typer.echo("prompt-only rejects implementation mode", err=True)
            raise typer.Exit(2)
        if mode not in ("analysis", "research"):
            mode = "research"
        if include_dirty or include_dirty_path:
            typer.echo("prompt-only rejects dirty/source flags", err=True)
            raise typer.Exit(2)
        if source is not None:
            typer.echo("prompt-only must not be combined with --source", err=True)
            raise typer.Exit(2)
    else:
        if source is None:
            typer.echo("run requires --source (or --prompt-only)", err=True)
            raise typer.Exit(2)
        if mode not in ("analysis", "implementation"):
            typer.echo("mode must be analysis or implementation", err=True)
            raise typer.Exit(2)
    if not prompt and not prompt_file:
        typer.echo("run requires --prompt or --prompt-file", err=True)
        raise typer.Exit(2)
    text = prompt_file.read_text(encoding="utf-8") if prompt_file else (prompt or "")
    if keep is not None and not str(keep).strip():
        typer.echo("--keep requires a nonempty reason", err=True)
        raise typer.Exit(2)
    if continue_task and backend != "native":
        typer.echo("--continue requires --backend native", err=True)
        raise typer.Exit(2)
    if write_continuation and keep is None:
        keep = "native continuation (TTL-bounded)"
    if continue_task and keep is not None and not write_continuation:
        typer.echo(
            "--continue with --keep also requires --write-continuation; omit both to finalize",
            err=True,
        )
        raise typer.Exit(2)
    execution = None
    if execution_manifest is not None:
        from grok_worker.execution_contract import (
            ExecutionContract,
            ExecutionContractError,
        )

        try:
            raw_exec = json.loads(execution_manifest.read_text(encoding="utf-8"))
            if not isinstance(raw_exec, dict):
                raise ExecutionContractError("execution manifest must be an object")
            # Accept full task manifest (with nested execution) or bare contract.
            nested = raw_exec.get("execution") if "execution" in raw_exec else raw_exec
            if not isinstance(nested, dict):
                raise ExecutionContractError("execution contract must be an object")
            execution = ExecutionContract.from_mapping(nested)
        except (OSError, json.JSONDecodeError, ExecutionContractError) as exc:
            typer.echo(f"invalid --execution-manifest: {exc}", err=True)
            raise typer.Exit(2) from exc
    disp_id = dispatcher_id or os.environ.get("GROK_WORKER_DISPATCHER_ID") or None
    if prompt_only:
        src: Path | None = None
        disp = (
            disposable_root.resolve()
            if disposable_root
            else (Path.cwd() / ".grok-disposable").resolve()
        )
    else:
        assert source is not None
        src = source.resolve()
        disp = disposable_root.resolve() if disposable_root else default_disposable_root(src)
    arts = artifact_root.resolve() if artifact_root else default_artifact_root(disp)
    cfg = RunConfig(
        source=src,
        prompt=text,
        disposable_root=disp,
        artifact_root=arts,
        shared_cache_root=_shared(shared_cache_root),
        cap_bytes=cap_bytes,
        keep_reason=keep,
        mode=mode,
        timeout=timeout,
        hard_timeout=None if hard_timeout == 0 else hard_timeout,
        task_id=task_id,
        run_id=run_id or make_run_id(),
        dispatcher_id=disp_id,
        acpx_bin=acpx_bin,
        agent_bin=agent_bin,
        mcp_config=mcp_config if mcp_config is not None else default_mcp_config(),
        model=model or default_model(),
        reasoning_effort=reasoning_effort or default_reasoning_effort(),
        allow_subagents=allow_subagents,
        failure_retain_hours=failure_retain_hours,
        max_workers=max_workers,
        prepare_deps=not no_prepare_deps and not prompt_only,
        include_dirty=include_dirty,
        include_dirty_paths=list(include_dirty_path or []),
        prompt_only=prompt_only,
        backend=backend,
        cache_max_bytes=cache_max_bytes,
        cache_ttl_hours=cache_ttl_hours,
        disable_web_search=disable_web_search,
        disallowed_tools=list(disallowed_tool or []),
        continue_task=continue_task,
        write_continuation=write_continuation,
        execution=execution,
        stall_turns=stall_turns,
        stall_seconds=stall_seconds,
        native_json_schema_result=not no_native_json_schema,
    )
    if detach:
        try:
            receipt = start_detached_run(cfg)
        except DetachedStartError as exc:
            _emit_startup_attention(cfg, exc, 1)
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        typer.echo(json.dumps(receipt, indent=2, sort_keys=True))
        return
    _execute_run_config(cfg)


def cmd_run_detached_child() -> None:
    """Internal detached child entry; accepts one RunConfig JSON object on stdin."""
    try:
        cfg = run_config_from_payload(json.loads(sys.stdin.read()))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        typer.echo(f"invalid detached run config: {exc}", err=True)
        raise typer.Exit(2) from exc
    _execute_run_config(cfg)


def cmd_preflight(
    source: Path = typer.Option(..., "--source", help="Repository or source tree to scan"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Scan all disclosure candidates once; report paths/rules, never values."""
    payload = disclosure_preflight(source.resolve())
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    elif payload["allowed"]:
        typer.echo(
            f"allowed dirty={payload.get('included_dirty_count', 0)} "
            f"excluded={payload.get('excluded_count', 0)}"
        )
    else:
        typer.echo(f"refused blocked={payload['blocked_count']}", err=True)
        for item in payload["blocked"]:
            typer.echo(
                f"  {item['path']} reason={item['reason_code']}",
                err=True,
            )
    if not payload["allowed"]:
        raise typer.Exit(2)


def cmd_lease_set(
    disposable_root: Path = typer.Option(..., "--disposable-root"),
    task_id: str = typer.Option(..., "--task-id"),
    idle_timeout: int | None = typer.Option(None, "--idle-timeout"),
    hard_timeout: int | None = typer.Option(
        None,
        "--hard-timeout",
        help="New absolute cap in seconds; 0 disables it",
    ),
) -> None:
    """Adjust a running worker lease without restarting its backend process."""
    if idle_timeout is None and hard_timeout is None:
        typer.echo("lease-set requires --idle-timeout and/or --hard-timeout", err=True)
        raise typer.Exit(2)
    root = disposable_root.resolve()
    matches: list[Path] = []
    if root.is_dir():
        for child in root.iterdir():
            if child.is_symlink() or not child.is_dir() or not is_managed_clone(child):
                continue
            try:
                meta = WorkerMeta.read(meta_path(child))
            except (OSError, ValueError, KeyError):
                continue
            if meta.task_id == task_id:
                matches.append(child)
    if len(matches) != 1:
        typer.echo(
            f"expected exactly one managed task {task_id!r}; found {len(matches)}",
            err=True,
        )
        raise typer.Exit(2)
    clone = matches[0]
    try:
        read_lease(clone)
        kwargs: dict[str, object] = {"idle_timeout_seconds": idle_timeout}
        if hard_timeout is not None:
            kwargs["hard_timeout_seconds"] = None if hard_timeout == 0 else hard_timeout
        state = set_lease_policy(clone, **kwargs)  # type: ignore[arg-type]
    except LeaseError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(state.to_dict(), indent=2, sort_keys=True))


def cmd_gc(
    disposable_root: Path | None = typer.Option(None, "--disposable-root"),
    shared_cache_root: Path | None = typer.Option(None, "--shared-cache-root"),
    source: Path | None = typer.Option(None, "--source"),
    artifact_root: Path | None = typer.Option(None, "--artifact-root"),
    tmp_age_hours: float = typer.Option(24.0, "--tmp-age-hours"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Clean lifecycle-managed clones and stale tmp entries."""
    root = _resolve_disposable(disposable_root, source)
    shared = _shared(shared_cache_root)
    arts = artifact_root.resolve() if artifact_root else default_artifact_root(root)
    protected: list[Path] = [shared, Path.home(), arts, root]
    if source is not None:
        protected.append(source.resolve())
    if root.is_dir():
        for child in root.iterdir():
            if not child.is_dir() or child.is_symlink() or not is_managed_clone(child):
                continue
            try:
                meta = WorkerMeta.read(meta_path(child))
            except (OSError, ValueError, KeyError):
                continue
            # Historical source paths from inactive workers must not pin a
            # reclaimable clone forever. Active locks/process identities still
            # protect their source; explicit --source remains protected above.
            if (
                meta.source_realpath
                and meta.source_realpath != "unknown-legacy"
                and is_active(meta, child)
            ):
                protected.append(Path(meta.source_realpath))
            if meta.artifact_path:
                protected.append(Path(meta.artifact_path))
    report = gc_disposable_root(
        root,
        protected=protected,
        tmp_age_hours=tmp_age_hours,
        shared_cache_root=shared,
    )
    cache_report = gc_shared_cache(CachePolicy(root=shared))
    payload = {
        "removed": report.removed,
        "retained": report.retained,
        "converted_dead": report.converted_dead,
        "skipped_legacy": report.skipped_legacy,
        "skipped_untrusted": report.skipped_untrusted,
        "tmp_removed": report.tmp_removed,
        "errors": report.errors,
        "shared_cache": {
            "root": cache_report.root,
            "before_bytes": cache_report.before_bytes,
            "after_bytes": cache_report.after_bytes,
            "max_bytes": cache_report.max_bytes,
            "removed": cache_report.removed,
            "protected": cache_report.protected,
            "errors": cache_report.errors,
        },
    }
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(f"removed: {len(report.removed)}")
        typer.echo(f"retained: {len(report.retained)}")
        typer.echo(f"converted_dead: {len(report.converted_dead)}")
        typer.echo(f"skipped_legacy: {len(report.skipped_legacy)}")
        typer.echo(f"tmp_removed: {len(report.tmp_removed)}")
        if report.errors:
            typer.echo(f"errors: {report.errors}")
        typer.echo(f"shared_cache_removed: {len(cache_report.removed)}")
        typer.echo(f"shared_cache_bytes: {cache_report.after_bytes}")


def cmd_status(
    disposable_root: Path | None = typer.Option(None, "--disposable-root"),
    shared_cache_root: Path | None = typer.Option(None, "--shared-cache-root"),
    source: Path | None = typer.Option(None, "--source"),
    cap_bytes: int = typer.Option(DEFAULT_CAP_BYTES, "--cap-bytes"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Report usage, cap, states (read-only)."""
    st = collect_status(
        _resolve_disposable(disposable_root, source),
        cap_bytes=cap_bytes,
        shared_cache_root=_shared(shared_cache_root),
    )
    typer.echo(format_status_json(st) if as_json else format_status_text(st), nl=False)


def cmd_import_legacy(
    name: str = typer.Option(..., "--name"),
    classification: str = typer.Option(..., "--classification"),
    disposable_root: Path | None = typer.Option(None, "--disposable-root"),
    reason: str | None = typer.Option(None, "--reason"),
    source_realpath: str | None = typer.Option(None, "--source-realpath"),
    artifact_root: Path | None = typer.Option(None, "--artifact-root"),
    confirm_expire: bool = typer.Option(False, "--confirm-expire"),
    base_commit: str | None = typer.Option(None, "--base-commit"),
) -> None:
    """Classify/import unmarked legacy clone (archive-before-delete for expire)."""
    if classification not in ("keep", "expire", "retain-24h"):
        typer.echo("classification must be keep|expire|retain-24h", err=True)
        raise typer.Exit(2)
    try:
        meta = import_legacy(
            _resolve_disposable(disposable_root, None),
            name,
            LegacyClass(classification),
            reason=reason,
            source_realpath=source_realpath,
            artifact_root=artifact_root,
            confirm_expire=confirm_expire,
            base_commit=base_commit,
        )
    except LegacyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(meta.to_dict(), indent=2))


def cmd_list_legacy(
    disposable_root: Path | None = typer.Option(None, "--disposable-root"),
    source: Path | None = typer.Option(None, "--source"),
) -> None:
    """List unmarked legacy direct children."""
    for n in list_unmarked(_resolve_disposable(disposable_root, source)):
        typer.echo(n)


def cmd_events(
    shared_cache_root: Path | None = typer.Option(None, "--shared-cache-root"),
    after: str = typer.Option("", "--after", help="Return events strictly after this event_id"),
    wait_seconds: float = typer.Option(
        DEFAULT_EVENT_WAIT_SECONDS,
        "--wait-seconds",
        help=f"Bounded long-poll seconds (default 30; 0=nonblocking; max {MAX_EVENT_WAIT_SECONDS})",
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Filter by run_id"),
    dispatcher_id: str | None = typer.Option(
        None, "--dispatcher-id", help="Filter by dispatcher_id"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Query shared-cache completion notifications (index only; lifecycle is truth)."""
    shared = _shared(shared_cache_root)
    try:
        events = list_completion_events(
            shared_cache_root=shared,
            after=after or "",
            wait_seconds=float(wait_seconds),
            run_id=run_id,
            dispatcher_id=dispatcher_id or os.environ.get("GROK_WORKER_DISPATCHER_ID") or None,
        )
    except EventWaitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    if as_json:
        typer.echo(json.dumps(events_to_payload(events), indent=2, sort_keys=True))
    else:
        for event in events:
            typer.echo(
                f"{event.get('event_id')} task={event.get('task_id')} "
                f"run={event.get('run_id')} state={event.get('state')} "
                f"ts={event.get('timestamp')}"
            )


def cmd_watch(
    shared_cache_root: Path | None = typer.Option(None, "--shared-cache-root"),
    disposable_root: Path | None = typer.Option(None, "--disposable-root"),
    source: Path | None = typer.Option(None, "--source"),
    after: str = typer.Option("", "--after", help="Return events after this event_id"),
    wait_seconds: float = typer.Option(
        DEFAULT_WATCH_WAIT_SECONDS,
        "--wait-seconds",
        help=f"Event-first wait seconds (default 300; max {MAX_EVENT_WAIT_SECONDS})",
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Watch one run"),
    dispatcher_id: str | None = typer.Option(
        None, "--dispatcher-id", help="Watch all runs for one dispatcher"
    ),
    until_settled: bool = typer.Option(
        False,
        "--until-settled",
        help="For one --run-id, keep the same event wait through cleanup settlement",
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Wait for completion/attention; return compact health only on timeout."""
    disp_id = dispatcher_id or os.environ.get("GROK_WORKER_DISPATCHER_ID") or None
    try:
        payload = watch_workers(
            shared_cache_root=_shared(shared_cache_root),
            disposable_root=_resolve_disposable(disposable_root, source),
            after=after or "",
            wait_seconds=float(wait_seconds),
            run_id=run_id,
            dispatcher_id=disp_id,
            until_settled=until_settled,
        )
    except (EventWaitError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if payload["kind"] == "events":
        for event in payload["events"]:
            typer.echo(
                f"{event.get('kind', 'terminal')} task={event.get('task_id')} "
                f"run={event.get('run_id')} state={event.get('state')} "
                f"attention={event.get('attention_required', False)}"
            )
        return
    typer.echo(
        f"heartbeat workers={len(payload['workers'])} attention={payload['attention_required']}"
    )
    for row in payload["workers"]:
        typer.echo(
            f"  task={row.get('task_id')} run={row.get('run_id')} "
            f"state={row.get('state')} phase={row.get('phase')} "
            f"last={row.get('last_activity_at')}"
        )


def cmd_health(
    disposable_root: Path | None = typer.Option(None, "--disposable-root"),
    source: Path | None = typer.Option(None, "--source"),
    dispatcher_id: str | None = typer.Option(None, "--dispatcher-id"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Diagnostic-only health inspection (read-only; never terminates workers)."""
    root = _resolve_disposable(disposable_root, source)
    report = collect_health(
        root,
        dispatcher_id=dispatcher_id or os.environ.get("GROK_WORKER_DISPATCHER_ID") or None,
    )
    payload = report.to_dict()
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(
            f"health diagnostic_only interval={report.interval_seconds}s "
            f"clones={len(report.clones)} (never mutates workers)"
        )
        for row in report.clones:
            typer.echo(
                f"  {row.get('task_id')} state={row.get('state')} "
                f"active={row.get('active')} elapsed={row.get('elapsed_seconds')}"
            )


def cmd_config_apply(
    config: Path = typer.Option(..., "--config", help="Live config path (must exist)"),
    candidate: Path = typer.Option(..., "--candidate", help="Candidate TOML path"),
    smoke_argv_json: str = typer.Option(
        ...,
        "--smoke-argv-json",
        help="JSON array of argv strings for smoke (shell=False)",
    ),
    smoke_timeout: float = typer.Option(30.0, "--smoke-timeout", help="Smoke timeout seconds"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Atomically apply a TOML candidate with smoke test and byte-level rollback."""
    try:
        code, receipt = apply_config(
            config_path=config,
            candidate_path=candidate,
            smoke_argv_json=smoke_argv_json,
            smoke_timeout=smoke_timeout,
        )
    except ConfigApplyError as exc:
        # Metadata-only error path — never echo config/candidate contents.
        err_receipt = {
            "config_path": str(config),
            "candidate_path": str(candidate),
            "error": "config_apply_refused",
            "rolled_back": False,
            "applied": False,
            "timed_out": False,
            "smoke_exit_code": None,
            "message": str(exc),
        }
        if as_json:
            typer.echo(json.dumps(err_receipt, indent=2, sort_keys=True))
        else:
            typer.echo(f"config-apply refused: {exc}", err=True)
        raise typer.Exit(1) from exc
    payload = receipt.to_dict()
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(
            f"applied={receipt.applied} rolled_back={receipt.rolled_back} "
            f"smoke_exit={receipt.smoke_exit_code} timed_out={receipt.timed_out}"
        )
    raise typer.Exit(int(code))
