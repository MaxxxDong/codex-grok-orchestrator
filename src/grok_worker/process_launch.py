"""Cross-platform subprocess launch policy."""

from __future__ import annotations

import os
import subprocess
from typing import Any


def hidden_startup_info() -> Any | None:
    """Hide Windows console children without detaching inherited stdio."""
    if os.name != "nt":
        return None
    platform_subprocess: Any = subprocess
    startup_info = platform_subprocess.STARTUPINFO()
    startup_info.dwFlags |= platform_subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = platform_subprocess.SW_HIDE
    return startup_info
