"""CLI commands for the immutable, grok-worker-owned acpx runtime."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer

from grok_worker.acpx_runtime import (
    AcpxRuntimeError,
    default_runtime_root,
    install_managed_runtime,
    resolve_managed_acpx_command,
)


def cmd_acpx_runtime_install(
    source_package: Path | None = typer.Option(
        None, "--source-package", help="Pinned acpx package root (default: resolve from PATH)"
    ),
    runtime_root: Path | None = typer.Option(None, "--runtime-root"),
) -> None:
    """Install the pinned Windows acpx build without modifying global npm files."""
    try:
        receipt = install_managed_runtime(
            source_package=source_package,
            runtime_root=runtime_root,
        )
    except AcpxRuntimeError as exc:
        typer.echo(f"acpx runtime install failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(asdict(receipt), indent=2, ensure_ascii=False))


def cmd_acpx_runtime_status(
    runtime_root: Path | None = typer.Option(None, "--runtime-root"),
) -> None:
    """Verify the current managed acpx runtime and print its resolved command."""
    root = (runtime_root or default_runtime_root()).resolve()
    try:
        command = resolve_managed_acpx_command(runtime_root=root)
    except AcpxRuntimeError as exc:
        typer.echo(
            json.dumps({"ok": False, "runtime_root": str(root), "error": str(exc)}, indent=2),
            err=True,
        )
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps(
            {"ok": True, "runtime_root": str(root), "command": command},
            indent=2,
            ensure_ascii=False,
        )
    )
