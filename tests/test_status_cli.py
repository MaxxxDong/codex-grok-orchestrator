"""CLI help/status smoke and installed-layout launcher."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from grok_worker.cli_cmds import _resolve_disposable

CANDIDATE = Path(__file__).resolve().parents[1]


def test_cli_help() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(CANDIDATE / "src")
    # Import typer from uv if needed
    proc = subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--with",
            "typer==0.15.2",
            "--with",
            "click==8.1.8",
            sys.executable,
            "-m",
            "grok_worker",
            "--help",
        ],
        cwd=str(CANDIDATE),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    assert "run" in proc.stdout or "lifecycle" in proc.stdout.lower()


def test_installed_launcher_arbitrary_cwd(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["GROK_WORKER_SKILL_ROOT"] = str(CANDIDATE)
    env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    launcher = CANDIDATE / "bin" / ("grok-worker.cmd" if os.name == "nt" else "grok-worker")
    proc = subprocess.run(
        [str(launcher), "status", "--disposable-root", str(tmp_path / "d")],
        cwd="/",
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "disposable_root" in proc.stdout or "usage_bytes" in proc.stdout


def test_default_status_root_matches_run_root_for_git_checkout(
    tmp_path: Path, monkeypatch,
) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    monkeypatch.chdir(checkout)

    assert _resolve_disposable(None, None) == (tmp_path / ".grok-disposable").resolve()
