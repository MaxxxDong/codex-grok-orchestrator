"""Fake acpx installer for tests (no network)."""

from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path


def write_fake_acpx(bin_dir: Path, behavior: str = "success") -> Path:
    """Install a fake `acpx` that simulates worker outcomes without network."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script_path = bin_dir / ("fake_acpx.py" if os.name == "nt" else "acpx")
    script = textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import json, os, sys, time
        from pathlib import Path

        behavior = os.environ.get("FAKE_ACPX_BEHAVIOR", {behavior!r})
        if os.environ.get("GROK_WORKER_LIFECYCLE") != "1":
            print("missing lifecycle environment", file=sys.stderr)
            sys.exit(92)
        cwd = None
        args = sys.argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--cwd" and i + 1 < len(args):
                cwd = Path(args[i + 1])
                i += 2
                continue
            i += 1
        if cwd is None:
            cwd = Path.cwd()
        out = cwd / ".grok-output"
        out.mkdir(parents=True, exist_ok=True)
        (out / "verification").mkdir(exist_ok=True)

        def write_result(**kwargs):
            (out / "result.json").write_text(
                json.dumps(kwargs) + "\\n", encoding="utf-8"
            )

        if behavior == "success":
            (cwd / "feature.txt").write_text("added\\n", encoding="utf-8")
            (cwd / "binary.bin").write_bytes(bytes(range(256)))
            (out / "verification" / "tests.txt").write_text("ok\\n", encoding="utf-8")
            write_result(
                schema_version=1,
                task_completed=True,
                status="completed",
                summary="implemented feature",
                findings=[],
                verification=[{{
                    "command": "pytest",
                    "exit_code": 0,
                    "log_path": ".grok-output/verification/tests.txt",
                }}],
            )
            print("fake acpx success")
            sys.exit(0)
        if behavior == "barrier_success":
            barrier = Path(os.environ["FAKE_ACPX_BARRIER_DIR"])
            barrier.mkdir(parents=True, exist_ok=True)
            task_id = os.environ["GROK_WORKER_TASK_ID"]
            (barrier / task_id).write_text("ready\\n", encoding="utf-8")
            expected = int(os.environ["FAKE_ACPX_BARRIER_EXPECTED"])
            deadline = time.monotonic() + 15
            while len(list(barrier.iterdir())) < expected and time.monotonic() < deadline:
                time.sleep(0.02)
            if len(list(barrier.iterdir())) < expected:
                print("parallel barrier timed out", file=sys.stderr)
                sys.exit(93)
            (cwd / "feature.txt").write_text("added\\n", encoding="utf-8")
            (out / "verification" / "tests.txt").write_text("ok\\n", encoding="utf-8")
            write_result(
                schema_version=1,
                task_completed=True,
                status="completed",
                summary="parallel worker completed",
                findings=[],
                verification=[{{
                    "command": "pytest",
                    "exit_code": 0,
                    "log_path": ".grok-output/verification/tests.txt",
                }}],
            )
            print("parallel fake acpx success")
            sys.exit(0)
        if behavior == "success_analysis":
            write_result(
                schema_version=1,
                task_completed=True,
                status="completed",
                summary="analysis done",
                findings=[{{"severity": "low", "msg": "nit"}}],
                verification=[],
            )
            print("fake analysis")
            sys.exit(0)
        if behavior == "success_review_findings":
            (out / "verification" / "tests.txt").write_text("ok\\n", encoding="utf-8")
            write_result(
                schema_version=1,
                task_completed=True,
                status="completed",
                summary="review done",
                findings=[{{"severity": "medium", "msg": "nits"}}],
                verification=[{{
                    "command": "lint",
                    "exit_code": 0,
                    "log_path": ".grok-output/verification/tests.txt",
                }}],
            )
            print("fake review")
            sys.exit(0)
        if behavior == "success_no_verify":
            write_result(
                schema_version=1,
                task_completed=True,
                status="completed",
                summary="no verify",
                findings=[],
                verification=[],
            )
            print("no verify")
            sys.exit(0)
        if behavior == "partial":
            (out / "verification" / "tests.txt").write_text("partial\\n", encoding="utf-8")
            write_result(
                schema_version=1,
                task_completed=True,
                status="partial",
                summary="partial work",
                findings=[],
                verification=[{{
                    "command": "pytest",
                    "exit_code": 0,
                    "log_path": ".grok-output/verification/tests.txt",
                }}],
            )
            print("partial")
            sys.exit(0)
        if behavior == "failed_verify":
            (out / "verification" / "tests.txt").write_text("FAIL\\n", encoding="utf-8")
            write_result(
                schema_version=1,
                task_completed=True,
                status="completed",
                summary="tests failed",
                findings=[],
                verification=[{{
                    "command": "pytest",
                    "exit_code": 1,
                    "log_path": ".grok-output/verification/tests.txt",
                }}],
            )
            print("verify fail")
            sys.exit(0)
        if behavior == "missing_verify_log":
            write_result(
                schema_version=1,
                task_completed=True,
                status="completed",
                summary="missing log",
                findings=[],
                verification=[{{
                    "command": "pytest",
                    "exit_code": 0,
                    "log_path": ".grok-output/verification/missing.txt",
                }}],
            )
            print("missing log")
            sys.exit(0)
        if behavior == "acpx_zero_no_result":
            print("acpx ok but no result")
            sys.exit(0)
        if behavior == "nonzero_completed":
            (out / "verification" / "tests.txt").write_text("ok\\n", encoding="utf-8")
            write_result(
                schema_version=1,
                task_completed=True,
                status="completed",
                summary="says completed but acpx failed",
                findings=[],
                verification=[{{
                    "command": "pytest",
                    "exit_code": 0,
                    "log_path": ".grok-output/verification/tests.txt",
                }}],
            )
            print("nonzero")
            sys.exit(2)
        if behavior == "failure":
            (cwd / "broken.txt").write_text("partial\\n", encoding="utf-8")
            (out / "verification" / "tests.txt").write_text("fail\\n", encoding="utf-8")
            write_result(
                schema_version=1,
                task_completed=False,
                status="failed",
                summary="could not finish",
                findings=[],
                verification=[{{
                    "command": "pytest",
                    "exit_code": 1,
                    "log_path": ".grok-output/verification/tests.txt",
                }}],
            )
            print("fake failure")
            sys.exit(1)
        if behavior == "crash":
            print("boom", file=sys.stderr)
            sys.exit(99)
        if behavior == "acp_runtime_internal_error":
            # Historical failure shape: no result.json, only acpx runtime error line.
            print("[acpx] error: RUNTIME Internal error")
            sys.exit(1)
        if behavior == "create_local_venv":
            (cwd / ".venv").mkdir(exist_ok=True)
            (cwd / ".venv" / "pyvenv.cfg").write_text("home = /x\\n", encoding="utf-8")
            (out / "verification" / "tests.txt").write_text("ok\\n", encoding="utf-8")
            write_result(
                schema_version=1,
                task_completed=True,
                status="completed",
                summary="made venv",
                findings=[],
                verification=[{{
                    "command": "pytest",
                    "exit_code": 0,
                    "log_path": ".grok-output/verification/tests.txt",
                }}],
            )
            sys.exit(0)
        print("unknown behavior", behavior, file=sys.stderr)
        sys.exit(2)
        """
    )
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    if os.name != "nt":
        return script_path
    launcher = bin_dir / "acpx.cmd"
    launcher.write_text(
        f'@"{sys.executable}" "{script_path}" %*\n',
        encoding="utf-8",
    )
    return launcher
