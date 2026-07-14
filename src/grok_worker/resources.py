"""Best-effort process resource snapshots without third-party deps.

Uses argv-direct ``ps`` with a short timeout. Any failure returns nulls.
"""

from __future__ import annotations

import subprocess
from typing import Any


def process_resources(pid: int | None) -> dict[str, float | int | None]:
    """Return ``{cpu_percent, rss_bytes}`` for *pid*, or null fields on failure."""
    empty: dict[str, float | int | None] = {"cpu_percent": None, "rss_bytes": None}
    if pid is None or pid <= 0:
        return empty
    try:
        proc = subprocess.run(
            ["ps", "-o", "%cpu=,rss=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return empty
    if proc.returncode != 0:
        return empty
    line = (proc.stdout or "").strip()
    if not line:
        return empty
    parts = line.split()
    if len(parts) < 2:
        return empty
    try:
        cpu = float(parts[0])
    except ValueError:
        cpu = None
    try:
        # ps rss is KiB on macOS and Linux procps.
        rss_kib = float(parts[1])
        rss_bytes = int(rss_kib * 1024)
    except ValueError:
        rss_bytes = None
    return {"cpu_percent": cpu, "rss_bytes": rss_bytes}


def empty_resources() -> dict[str, Any]:
    return {"cpu_percent": None, "rss_bytes": None}
