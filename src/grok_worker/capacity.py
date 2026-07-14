"""Disposable-root usage accounting, concurrency, and creation-time cap enforcement."""

from __future__ import annotations

from pathlib import Path

from grok_worker.constants import (
    DEFAULT_CAP_BYTES,
    MAX_CONCURRENT_WORKERS,
    ROOT_LOCK_NAME,
)
from grok_worker.models import WorkerMeta, WorkerState, meta_is_trusted
from grok_worker.paths import is_managed_clone, meta_path
from grok_worker.safety import dir_size_bytes


class CapacityError(RuntimeError):
    """Raised when disposable root exceeds the configured cap."""

    def __init__(self, usage: int, cap: int, root: Path) -> None:
        self.usage = usage
        self.cap = cap
        self.root = root
        super().__init__(
            f"disposable root {root} usage {usage} bytes exceeds cap {cap} bytes; "
            "refusing to create a new worker"
        )


class ConcurrencyError(RuntimeError):
    """Raised when concurrent worker limit is reached."""

    def __init__(self, active: int, limit: int) -> None:
        self.active = active
        self.limit = limit
        super().__init__(
            f"concurrent workers {active} at or over configured limit {limit}; "
            "refusing new worker"
        )


def root_usage_bytes(disposable_root: Path) -> int:
    """Sum sizes of all non-symlink direct children, including dot directories.

    Only the small lifecycle lock file may be special-cased (counted by size,
    never skipped as a tree). Unmarked legacy bytes count toward the cap.
    """
    root = disposable_root
    if not root.is_dir():
        return 0
    total = 0
    for child in root.iterdir():
        if child.is_symlink():
            continue
        if child.name == ROOT_LOCK_NAME and child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
            continue
        total += dir_size_bytes(child)
    return total


def enforce_cap(disposable_root: Path, cap_bytes: int = DEFAULT_CAP_BYTES) -> int:
    usage = root_usage_bytes(disposable_root)
    if usage > cap_bytes:
        raise CapacityError(usage, cap_bytes, disposable_root)
    return usage


ACTIVE_STATES = frozenset(
    {
        WorkerState.CREATING,
        WorkerState.RUNNING,
        WorkerState.FINALIZING,
    }
)


def count_active_workers(disposable_root: Path) -> int:
    """Count managed clones in creating/running/finalizing states."""
    root = disposable_root
    if not root.is_dir():
        return 0
    n = 0
    for child in root.iterdir():
        if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
            continue
        if not is_managed_clone(child):
            continue
        try:
            meta = WorkerMeta.read(meta_path(child))
        except (OSError, ValueError, KeyError):
            continue
        if not meta_is_trusted(meta):
            continue
        if meta.state in ACTIVE_STATES:
            n += 1
    return n


def enforce_concurrency(disposable_root: Path, limit: int = MAX_CONCURRENT_WORKERS) -> int:
    active = count_active_workers(disposable_root)
    if active >= limit:
        raise ConcurrencyError(active, limit)
    return active
