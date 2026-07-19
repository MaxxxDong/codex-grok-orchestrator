"""Portable Grok ACP agent entry point used by acpx."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from grok_worker.process_launch import hidden_startup_info
from grok_worker.run_config import check_grok_environment
from grok_worker.settings import default_model, default_reasoning_effort, env_flag, env_text


def resolve_grok_bin(
    *,
    platform: str | None = None,
    home: Path | None = None,
    path_lookup: Callable[[str], str | None] = shutil.which,
) -> str:
    """Prefer xAI's canonical native binary over the Windows npm trampoline."""
    effective_platform = platform or os.name
    if effective_platform == "nt":
        native = (home or Path.home()) / ".grok" / "bin" / "grok.exe"
        if native.is_file():
            return str(native)
    return path_lookup("grok") or "grok"


def build_command() -> list[str]:
    configured = os.environ.get("GROK_WORKER_GROK_BIN")
    grok_bin = configured.strip() if configured else resolve_grok_bin()
    if not grok_bin:
        raise ValueError("GROK_WORKER_GROK_BIN must not be empty")
    command = [
        grok_bin,
        "--sandbox",
        env_text("GROK_WORKER_SANDBOX", "workspace"),
        "--always-approve",
        "--model",
        default_model(),
        "--reasoning-effort",
        default_reasoning_effort(),
    ]
    if not env_flag("GROK_WORKER_ALLOW_SUBAGENTS", default=True):
        command.append("--no-subagents")
    leader_socket = os.environ.get("GROK_WORKER_LEADER_SOCKET") or str(
        Path(tempfile.gettempdir()) / f"grok-worker-{os.getpid()}.sock"
    )
    command.extend(["agent", "stdio"])
    command.extend(["--leader-socket", leader_socket])
    return command


def _child_environment() -> dict[str, str]:
    env = os.environ.copy()
    if os.name == "nt":
        # The official npm trampoline sets this before starting grok.exe.
        env["GROK_MANAGED_BY_NPM"] = "1"
    return env


def main() -> int:
    if not os.environ.get("GROK_WORKER_LIFECYCLE") and not env_flag(
        "GROK_WORKER_ALLOW_DIRECT_AGENT", default=False
    ):
        print(
            "grok-worker-agent: refusing direct invocation without "
            "GROK_WORKER_LIFECYCLE=1",
            file=sys.stderr,
        )
        return 2
    try:
        command = build_command()
        socket_path = Path(command[command.index("--leader-socket") + 1])
        child_env = _child_environment()
        warning = check_grok_environment(command[0], cwd=Path.cwd(), environ=child_env)
        if warning:
            print(f"grok-worker-agent: warning: {warning}", file=sys.stderr)
        completed = subprocess.run(
            command,
            env=child_env,
            check=False,
            startupinfo=hidden_startup_info(),
            creationflags=(
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if os.name == "nt"
                else 0
            ),
        )
    except (OSError, ValueError) as exc:
        print(f"grok-worker-agent: {exc}", file=sys.stderr)
        return 127
    finally:
        if "socket_path" in locals():
            socket_path.unlink(missing_ok=True)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
