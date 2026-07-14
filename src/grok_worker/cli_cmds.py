"""CLI command implementations for the Typer app."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from grok_worker.cache_policy import (
    DEFAULT_CACHE_MAX_BYTES,
    DEFAULT_CACHE_TTL_HOURS,
    CacheCapacityError,
    CachePolicy,
    gc_shared_cache,
)
from grok_worker.capacity import CapacityError, ConcurrencyError
from grok_worker.completion_events import events_to_payload, list_completion_events
from grok_worker.config_apply import ConfigApplyError, apply_config
from grok_worker.constants import DEFAULT_CAP_BYTES, DEFAULT_FAILURE_RETAIN_HOURS
from grok_worker.gc import gc_disposable_root, is_active
from grok_worker.legacy import LegacyClass, LegacyError, import_legacy, list_unmarked
from grok_worker.models import WorkerMeta
from grok_worker.paths import (
    default_artifact_root,
    default_disposable_root,
    default_shared_cache_root,
    is_managed_clone,
    meta_path,
)
from grok_worker.runner import RunConfig, run_worker
from grok_worker.settings import default_mcp_config, default_model, default_reasoning_effort
from grok_worker.status import collect_status, format_status_json, format_status_text


def _shared(shared_cache_root: Path | None) -> Path:
    if shared_cache_root is not None:
        return Path(shared_cache_root).resolve()
    return default_shared_cache_root()


def _resolve_disposable(disposable_root: Path | None, source: Path | None) -> Path:
    if disposable_root is not None:
        return Path(disposable_root).resolve()
    if source is not None:
        return default_disposable_root(Path(source)).resolve()
    return (Path.cwd() / ".grok-disposable").resolve()


def cmd_run(
    source: Path = typer.Option(..., "--source", help="Source repository or tree"),
    prompt: str | None = typer.Option(None, "--prompt"),
    prompt_file: Path | None = typer.Option(None, "--prompt-file"),
    disposable_root: Path | None = typer.Option(None, "--disposable-root"),
    artifact_root: Path | None = typer.Option(None, "--artifact-root"),
    shared_cache_root: Path | None = typer.Option(None, "--shared-cache-root"),
    keep: str | None = typer.Option(None, "--keep", help="Nonempty reason to retain"),
    mode: str = typer.Option("implementation", "--mode"),
    timeout: int = typer.Option(1800, "--timeout"),
    task_id: str | None = typer.Option(None, "--task-id"),
    acpx_bin: str = typer.Option("acpx", "--acpx-bin"),
    agent_bin: str | None = typer.Option(None, "--agent-bin"),
    mcp_config: str | None = typer.Option(None, "--mcp-config"),
    model: str | None = typer.Option(None, "--model"),
    reasoning_effort: str | None = typer.Option(None, "--reasoning-effort"),
    allow_subagents: bool = typer.Option(False, "--allow-subagents"),
    failure_retain_hours: int = typer.Option(
        DEFAULT_FAILURE_RETAIN_HOURS, "--failure-retain-hours"
    ),
    cap_bytes: int = typer.Option(DEFAULT_CAP_BYTES, "--cap-bytes"),
    no_prepare_deps: bool = typer.Option(False, "--no-prepare-deps"),
    include_dirty: bool = typer.Option(
        False, "--include-dirty", help="Copy dirty source state as clone baseline"
    ),
    cache_max_bytes: int = typer.Option(DEFAULT_CACHE_MAX_BYTES, "--cache-max-bytes"),
    cache_ttl_hours: float = typer.Option(DEFAULT_CACHE_TTL_HOURS, "--cache-ttl-hours"),
) -> None:
    """Create clone, run acpx, collect artifacts, finalize."""
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
        task_id=task_id,
        acpx_bin=acpx_bin,
        agent_bin=agent_bin,
        mcp_config=mcp_config if mcp_config is not None else default_mcp_config(),
        model=model or default_model(),
        reasoning_effort=reasoning_effort or default_reasoning_effort(),
        allow_subagents=allow_subagents,
        failure_retain_hours=failure_retain_hours,
        prepare_deps=not no_prepare_deps,
        include_dirty=include_dirty,
        cache_max_bytes=cache_max_bytes,
        cache_ttl_hours=cache_ttl_hours,
    )
    try:
        outcome = run_worker(cfg)
    except (CapacityError, ConcurrencyError, CacheCapacityError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"run failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps(
            {
                "task_id": outcome.task_id,
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
        0.0, "--wait-seconds", help="Bounded long-poll seconds (0 = immediate)"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Query shared-cache completion notifications (index only; lifecycle is truth)."""
    shared = _shared(shared_cache_root)
    events = list_completion_events(
        shared_cache_root=shared,
        after=after or "",
        wait_seconds=max(0.0, float(wait_seconds)),
    )
    if as_json:
        typer.echo(json.dumps(events_to_payload(events), indent=2, sort_keys=True))
    else:
        for event in events:
            typer.echo(
                f"{event.get('event_id')} task={event.get('task_id')} "
                f"state={event.get('state')} ts={event.get('timestamp')}"
            )


def cmd_config_apply(
    config: Path = typer.Option(..., "--config", help="Live config path (must exist)"),
    candidate: Path = typer.Option(..., "--candidate", help="Candidate TOML path"),
    smoke_argv_json: str = typer.Option(
        ...,
        "--smoke-argv-json",
        help="JSON array of argv strings for smoke (shell=False)",
    ),
    smoke_timeout: float = typer.Option(
        30.0, "--smoke-timeout", help="Smoke timeout seconds"
    ),
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
