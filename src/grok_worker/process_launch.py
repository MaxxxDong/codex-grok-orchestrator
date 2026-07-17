"""Cross-platform subprocess launch policy."""

from __future__ import annotations

import os
import subprocess


def hidden_startup_info() -> subprocess.STARTUPINFO | None:
    """Hide Windows console children without detaching inherited stdio."""
    if os.name != "nt":
        return None
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    return startup_info
