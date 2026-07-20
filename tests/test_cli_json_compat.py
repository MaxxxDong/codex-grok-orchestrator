"""CLI compatibility: --json flags and concise Click usage errors."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from grok_worker.cli import app, main


def test_cache_status_accepts_json_flag(tmp_path: Path, capsys) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    code = main(
        [
            "cache-status",
            "--shared-cache-root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["root"] == str(root.resolve())
    assert "usage_bytes" in payload
    assert payload["over_limit"] is False


def test_cache_gc_accepts_json_flag(tmp_path: Path, capsys) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    code = main(
        [
            "cache-gc",
            "--shared-cache-root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["root"] == str(root.resolve())
    assert "before_bytes" in payload
    assert "after_bytes" in payload


def test_invalid_option_exits_concise_usage_error_not_traceback(capsys) -> None:
    """Real entry point: unknown option must not surface a Python traceback."""
    code = main(["cache-status", "--not-a-real-option"])
    assert code != 0
    err = capsys.readouterr().err
    assert "No such option" in err or "no such option" in err.lower()
    assert "Traceback" not in err
    assert "Rich" not in err
    assert 'File "' not in err


def test_cli_runner_cache_status_json(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / "shared"
    root.mkdir()
    ok = runner.invoke(
        app,
        ["cache-status", "--shared-cache-root", str(root), "--json"],
    )
    assert ok.exit_code == 0
    payload = json.loads(ok.stdout)
    assert payload["root"] == str(root.resolve())


def test_cli_runner_rejects_unknown_option_without_python_traceback() -> None:
    runner = CliRunner()
    bad = runner.invoke(app, ["cache-status", "--nope"])
    assert bad.exit_code != 0
    # CliRunner may merge streams; assert no Python stack-trace-shaped output.
    assert "Traceback (most recent call last)" not in (bad.output or "")
    assert bad.exception is None or type(bad.exception).__name__ in {
        "NoSuchOption",
        "UsageError",
        "ClickException",
        "SystemExit",
    }


def test_run_rejects_removed_max_turns_option() -> None:
    runner = CliRunner()
    bad = runner.invoke(app, ["run", "--max-turns", "12"])

    assert bad.exit_code != 0
    assert "No such option" in (bad.output or "")
    assert "--max-turns" in (bad.output or "")
