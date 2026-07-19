"""Typed Typer CLI entry; command bodies live in cli_cmds."""

from __future__ import annotations

import sys

import typer

from grok_worker import __version__, cache_cmds, cli_cmds, session_cli

app = typer.Typer(
    name="grok-worker",
    help="Lifecycle runner for native Grok headless and ACP compatibility workers.",
    add_completion=False,
    no_args_is_help=True,
    invoke_without_command=True,
)

app.command("run")(cli_cmds.cmd_run)
app.command("preflight")(cli_cmds.cmd_preflight)
app.command("gc")(cli_cmds.cmd_gc)
app.command("status")(cli_cmds.cmd_status)
app.command("events")(cli_cmds.cmd_events)
app.command("watch")(cli_cmds.cmd_watch)
app.command("health")(cli_cmds.cmd_health)
app.command("lease-set")(cli_cmds.cmd_lease_set)
app.command("config-apply")(cli_cmds.cmd_config_apply)
app.command("import-legacy")(cli_cmds.cmd_import_legacy)
app.command("list-legacy")(cli_cmds.cmd_list_legacy)
app.command("cache-status")(cache_cmds.cmd_cache_status)
app.command("cache-gc")(cache_cmds.cmd_cache_gc)
app.command("session-start")(session_cli.cmd_session_start)
app.command("session-followup")(session_cli.cmd_session_followup)
app.command("session-finalize")(session_cli.cmd_session_finalize)


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", is_eager=True, help="Show version and exit."),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


def main(argv: list[str] | None = None) -> int:
    """Entry point compatible with python -m grok_worker."""
    try:
        # standalone_mode=False: Typer/Click returns the exit code instead of
        # raising SystemExit; propagate it so process exit matches command result.
        result = app(args=argv, standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code) if exc.exit_code is not None else 0
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 1
    if result is None:
        return 0
    if isinstance(result, int):
        return result
    return 0


if __name__ == "__main__":
    sys.exit(main())
