"""Small shared registry for disposable roots used by local workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from grok_worker.locks import FileLock
from grok_worker.models import atomic_write_text, dt_to_iso, utc_now

_REGISTRY_DIR = "runtime"
_REGISTRY_FILE = "disposable-roots.json"
_REGISTRY_LOCK = "disposable-roots.lock"
_MAX_REGISTERED_ROOTS = 256


def _registry_path(shared_cache_root: Path) -> Path:
    return shared_cache_root.resolve() / _REGISTRY_DIR / _REGISTRY_FILE


def _lock_path(shared_cache_root: Path) -> Path:
    return shared_cache_root.resolve() / "locks" / _REGISTRY_LOCK


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _read_entries(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("roots") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    return [item for item in entries if isinstance(item, dict)]


def register_disposable_root(shared_cache_root: Path, disposable_root: Path) -> None:
    """Persist one root so default health can see workers outside the current cwd."""
    shared = shared_cache_root.resolve()
    root = disposable_root.resolve()
    path = _registry_path(shared)
    with FileLock(_lock_path(shared)):
        by_key: dict[str, dict[str, str]] = {}
        for item in _read_entries(path):
            value = item.get("path")
            if not isinstance(value, str) or not value:
                continue
            candidate = Path(value)
            by_key[_path_key(candidate)] = {
                "path": str(candidate.resolve()),
                "updated_at": str(item.get("updated_at") or ""),
            }
        by_key[_path_key(root)] = {
            "path": str(root),
            "updated_at": dt_to_iso(utc_now()) or "",
        }
        entries = sorted(
            by_key.values(),
            key=lambda item: item["updated_at"],
            reverse=True,
        )[:_MAX_REGISTERED_ROOTS]
        atomic_write_text(
            path,
            json.dumps({"version": 1, "roots": entries}, indent=2, sort_keys=True) + "\n",
        )


def known_disposable_roots(shared_cache_root: Path) -> list[Path]:
    """Return registered roots without mutating or probing their contents."""
    entries = _read_entries(_registry_path(shared_cache_root.resolve()))
    roots: dict[str, Path] = {}
    for item in entries:
        value = item.get("path")
        if not isinstance(value, str) or not value:
            continue
        root = Path(value).resolve()
        if not root.is_dir():
            continue
        roots[_path_key(root)] = root
    return sorted(roots.values(), key=lambda path: str(path).casefold())


__all__ = ["known_disposable_roots", "register_disposable_root"]
