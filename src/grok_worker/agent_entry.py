"""Portable Grok ACP agent entry point used by acpx."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from grok_worker.settings import default_model, default_reasoning_effort, env_flag, env_text


def build_command() -> list[str]:
    configured = os.environ.get("GROK_WORKER_GROK_BIN")
    grok_bin = configured.strip() if configured else (shutil.which("grok") or "grok")
    if not grok_bin:
        raise ValueError("GROK_WORKER_GROK_BIN must not be empty")
    command = [
        grok_bin,
        "--sandbox",
        env_text("GROK_WORKER_SANDBOX", "workspace"),
        "--model",
        default_model(),
        "--reasoning-effort",
        default_reasoning_effort(),
    ]
    if not env_flag("GROK_WORKER_ALLOW_SUBAGENTS", default=False):
        command.append("--no-subagents")
    leader_socket = os.environ.get("GROK_WORKER_LEADER_SOCKET") or str(
        Path(tempfile.gettempdir()) / f"grok-worker-{os.getpid()}.sock"
    )
    command.extend(["agent", "stdio"])
    command.extend(["--leader-socket", leader_socket])
    return command


def _creation_flags() -> int:
    """Keep the Windows .cmd launcher attached but visually silent."""
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main() -> int:
    if not os.environ.get("GROK_WORKER_LIFECYCLE") and not env_flag(
        "GROK_WORKER_ALLOW_DIRECT_AGENT", default=False
    ):
        print(
            "grok-worker-agent: refusing direct invocation without GROK_WORKER_LIFECYCLE=1",
            file=sys.stderr,
        )
        return 2
    try:
        command = build_command()
        socket_path = Path(command[command.index("--leader-socket") + 1])
        completed = subprocess.run(command, check=False, creationflags=_creation_flags())
    except (OSError, ValueError) as exc:
        print(f"grok-worker-agent: {exc}", file=sys.stderr)
        return 127
    finally:
        if "socket_path" in locals():
            socket_path.unlink(missing_ok=True)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
