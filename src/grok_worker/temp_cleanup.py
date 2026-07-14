"""Safe cleanup of stale direct-child grok-* entries under /tmp and $TMPDIR."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from grok_worker.constants import DEFAULT_TMP_AGE_HOURS, TMP_GROK_PREFIX
from grok_worker.models import WorkerMeta
from grok_worker.paths import meta_path
from grok_worker.process_identity import process_matches
from grok_worker.safety import SafetyError, safe_rmtree, safe_unlink


def _tmp_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for raw in (os.environ.get("TMPDIR"), tempfile.gettempdir()):
        if not raw:
            continue
        p = Path(raw)
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if p.is_dir():
            roots.append(p)
    return roots


def _recorded_identity(path: Path) -> tuple[int | None, str | None]:
    mp = meta_path(path)
    if not mp.is_file():
        return None, None
    try:
        meta = WorkerMeta.read(mp)
    except (OSError, ValueError, KeyError):
        return None, None
    return meta.runner_pid or meta.pid, meta.runner_start_token


def clean_stale_tmp(
    *,
    age_hours: float = DEFAULT_TMP_AGE_HOURS,
    now: float | None = None,
    protected: list[Path] | None = None,
) -> list[str]:
    """Remove old direct children matching grok-* that are not live and not symlinks."""
    threshold = (now if now is not None else time.time()) - age_hours * 3600
    removed: list[str] = []
    prot = list(protected or [])
    for root in _tmp_roots():
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.name.startswith(TMP_GROK_PREFIX):
                continue
            if child.is_symlink():
                continue
            try:
                if not child.is_dir() and not child.is_file():
                    continue
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if mtime > threshold:
                continue
            pid, token = _recorded_identity(child)
            if process_matches(pid, token):
                continue
            try:
                if child.is_dir():
                    safe_rmtree(child, disposable_root=root, protected=prot)
                else:
                    safe_unlink(child, disposable_root=root, protected=prot)
                removed.append(str(child))
            except (SafetyError, OSError):
                continue
    return removed
