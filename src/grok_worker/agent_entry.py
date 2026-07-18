"""Portable Grok ACP agent entry point used by acpx."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from grok_worker.grok_profile import (
    GrokProfileError,
    isolated_child_environment,
    prepare_isolated_profile,
    validate_isolated_profile,
)
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
        profile = prepare_isolated_profile(
            model_id=default_model(),
            reasoning_effort=default_reasoning_effort(),
        )
        child_env = isolated_child_environment(os.environ, profile)
        validate_isolated_profile(
            grok_bin=command[0],
            profile=profile,
            environ=child_env,
            cwd=Path.cwd(),
            allow_extensions=env_flag("GROK_WORKER_ALLOW_GROK_EXTENSIONS", default=False),
        )
        completed = subprocess.run(command, env=child_env, check=False)
    except (GrokProfileError, OSError, ValueError) as exc:
        print(f"grok-worker-agent: {exc}", file=sys.stderr)
        return 127
    finally:
        if "socket_path" in locals():
            socket_path.unlink(missing_ok=True)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
