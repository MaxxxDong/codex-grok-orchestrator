"""Stable process identity (PID + birth token) for GC liveness checks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def process_start_token(pid: int) -> str | None:
    """Return a stable start token for *pid*, or None if unavailable."""
    if pid <= 0:
        return None
    # Prefer /proc on Linux
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            # Field 22 is starttime (clock ticks after boot)
            data = stat_path.read_text(encoding="utf-8", errors="replace")
            # comm may contain spaces/parens; split after last ')'
            rparen = data.rfind(")")
            if rparen != -1:
                fields = data[rparen + 2 :].split()
                if len(fields) >= 20:
                    return f"proc:{fields[19]}"
        except OSError:
            pass
    try:
        out = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return out or None
    except (OSError, subprocess.CalledProcessError):
        return None


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_matches(pid: int | None, start_token: str | None) -> bool:
    """True only when pid is live *and* birth identity matches.

    Missing start_token means identity cannot be verified → not a match.
    """
    if pid is None or pid <= 0:
        return False
    if not start_token:
        return False
    if not pid_exists(pid):
        return False
    current = process_start_token(pid)
    if current is None:
        return False
    return current == start_token


def capture_identity(pid: int | None = None) -> tuple[int | None, str | None]:
    """Capture (pid, start_token) for the given or current process."""
    p = os.getpid() if pid is None else pid
    if p is None or p <= 0:
        return None, None
    return p, process_start_token(p)
