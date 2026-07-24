"""Typer entry points for lifecycle-managed named ACP sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from grok_worker.constants import DEFAULT_ACPX_TIMEOUT, DEFAULT_HARD_TIMEOUT, MAX_CONCURRENT_WORKERS
from grok_worker.paths import (
    default_artifact_root,
    default_disposable_root,
    default_shared_cache_root,
)
from grok_worker.session_process import SessionConfig, SessionOutcome
from grok_worker.session_runtime import finalize_session, followup_session, start_session
from grok_worker.settings import default_mcp_config, default_model, default_reasoning_effort


def _config(
    source: Path,
    manifest_file: Path,
    role: str,
    mode: str,
    disposable_root: Path | None,
    artifact_root: Path | None,
    shared_cache_root: Path | None,
    acpx_bin: str | None,
    agent_bin: str | None,
    mcp_config: str | None,
    model: str | None,
    reasoning_effort: str | None,
    allow_subagents: bool,
    timeout: int,
    hard_timeout: int,
    max_workers: int,
    no_prepare_deps: bool,
) -> SessionConfig:
    resolved_source = source.resolve()
    disposable = (
        disposable_root.resolve()
        if disposable_root is not None
        else default_disposable_root(resolved_source).resolve()
    )
    artifact = (
        artifact_root.resolve()
        if artifact_root is not None
        else default_artifact_root(disposable).resolve()
    )
    return SessionConfig(
        source=resolved_source,
        manifest_file=manifest_file.resolve(),
        role=role,
        mode=mode,
        disposable_root=disposable,
        artifact_root=artifact,
        shared_cache_root=(
            shared_cache_root.resolve()
            if shared_cache_root is not None
            else default_shared_cache_root()
        ),
        acpx_bin=acpx_bin,
        agent_bin=agent_bin,
        mcp_config=mcp_config if mcp_config is not None else default_mcp_config(),
        model=model or default_model(),
        reasoning_effort=reasoning_effort or default_reasoning_effort(),
        allow_subagents=allow_subagents,
        timeout=timeout,
        hard_timeout=None if hard_timeout == 0 else hard_timeout,
        max_workers=max_workers,
        prepare_deps=not no_prepare_deps,
    )


def _emit(outcome: SessionOutcome) -> None:
    typer.echo(json.dumps(outcome.__dict__, indent=2))


def _run(action: str, cfg: SessionConfig) -> None:
    try:
        if action == "start":
            outcome = start_session(cfg)
        elif action == "followup":
            outcome = followup_session(cfg)
        else:
            outcome = finalize_session(cfg)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"session {action} failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    _emit(outcome)
    if outcome.state in {"failed", "session_error"}:
        raise typer.Exit(1)


def _options(
    source: Path,
    manifest_file: Path,
    role: str,
    mode: str,
    disposable_root: Path | None,
    artifact_root: Path | None,
    shared_cache_root: Path | None,
    acpx_bin: str | None,
    agent_bin: str | None,
    mcp_config: str | None,
    model: str | None,
    reasoning_effort: str | None,
    allow_subagents: bool,
    timeout: int,
    hard_timeout: int,
    max_workers: int,
    no_prepare_deps: bool,
) -> SessionConfig:
    return _config(
        source,
        manifest_file,
        role,
        mode,
        disposable_root,
        artifact_root,
        shared_cache_root,
        acpx_bin,
        agent_bin,
        mcp_config,
        model,
        reasoning_effort,
        allow_subagents,
        timeout,
        hard_timeout,
        max_workers,
        no_prepare_deps,
    )


def _session_command(action: str):  # type: ignore[no-untyped-def]
    def command(
        source: Path = typer.Option(..., "--source"),
        manifest_file: Path = typer.Option(..., "--manifest-file"),
        role: str = typer.Option("implement", "--role"),
        mode: str = typer.Option("implementation", "--mode"),
        disposable_root: Path | None = typer.Option(None, "--disposable-root"),
        artifact_root: Path | None = typer.Option(None, "--artifact-root"),
        shared_cache_root: Path | None = typer.Option(None, "--shared-cache-root"),
        acpx_bin: str | None = typer.Option(
            None,
            "--acpx-bin",
            help="Explicit acpx override (default: pinned grok-worker runtime on Windows)",
        ),
        agent_bin: str | None = typer.Option(None, "--agent-bin"),
        mcp_config: str | None = typer.Option(None, "--mcp-config"),
        model: str | None = typer.Option(None, "--model"),
        reasoning_effort: str | None = typer.Option(None, "--reasoning-effort"),
        allow_subagents: bool = typer.Option(False, "--allow-subagents"),
        timeout: int = typer.Option(
            DEFAULT_ACPX_TIMEOUT,
            "--timeout",
            help="Inactivity lease seconds; real activity renews it",
        ),
        hard_timeout: int = typer.Option(
            DEFAULT_HARD_TIMEOUT,
            "--hard-timeout",
            help="Absolute safety cap seconds; 0 disables it",
        ),
        max_workers: int = typer.Option(
            MAX_CONCURRENT_WORKERS,
            "--max-workers",
            envvar="GROK_WORKER_MAX_WORKERS",
            min=1,
        ),
        no_prepare_deps: bool = typer.Option(False, "--no-prepare-deps"),
        dispatcher_id: str | None = typer.Option(None, "--dispatcher-id"),
        run_id: str | None = typer.Option(None, "--run-id"),
    ) -> None:
        """Operate one immutable logical-task named session."""
        cfg = _options(
            source,
            manifest_file,
            role,
            mode,
            disposable_root,
            artifact_root,
            shared_cache_root,
            acpx_bin,
            agent_bin,
            mcp_config,
            model,
            reasoning_effort,
            allow_subagents,
            timeout,
            hard_timeout,
            max_workers,
            no_prepare_deps,
        )
        cfg.dispatcher_id = dispatcher_id or os.environ.get("GROK_WORKER_DISPATCHER_ID") or None
        cfg.run_id = run_id
        _run(action, cfg)

    command.__name__ = f"cmd_session_{action}"
    return command


cmd_session_start = _session_command("start")
cmd_session_followup = _session_command("followup")
cmd_session_finalize = _session_command("finalize")
