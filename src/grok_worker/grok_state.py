"""Bounded cleanup for Grok state created by disposable clone paths."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

from grok_worker.safety import safe_rmtree
from grok_worker.settings import grok_home


def clone_session_root(
    clone: Path, environ: Mapping[str, str] = os.environ
) -> Path:
    encoded = quote(str(clone.resolve()), safe="")
    return grok_home(environ) / "sessions" / encoded


def cleanup_clone_session_state(
    clone: Path, environ: Mapping[str, str] = os.environ
) -> bool:
    """Delete only the native session bucket owned by one disposable clone."""
    target = clone_session_root(clone, environ)
    if not target.exists() and not target.is_symlink():
        return False
    safe_rmtree(target, disposable_root=target.parent, protected=[])
    return True
