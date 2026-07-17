"""Portable filesystem helpers used only by the test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def symlink_or_skip(
    link: Path,
    target: Path | str,
    *,
    target_is_directory: bool = False,
) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
