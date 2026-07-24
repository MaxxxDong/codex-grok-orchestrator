"""Per-dispatcher concurrency via fixed OS flock slot leases under shared cache.

When ``dispatcher_id`` is set, capacity is reserved by a non-blocking exclusive
platform lock on one of the configured fixed slot files (10 by default):

    $CACHE/dispatchers/<dispatcher_hash>/slots/00.lock .. 09.lock

Acquiring a slot lock is the atomic capacity reservation. The ``FileLock`` is
held for the entire active CLI / ACP invocation and released in ``finally``;
process crash releases the lease automatically via OS flock semantics. There is
no machine-global limit: other dispatcher IDs use different hash directories and
never count or block one another.

Implementation mode additionally acquires a non-blocking source exclusion lock
under the same dispatcher hash, keyed by the canonical source hash (never a raw
path). Analysis does not take the source lock.

Without ``dispatcher_id``, callers use root-scoped counting only (backward
compatible; no silent cross-root claim).

This module stores only lock files and identity hashes — never prompts, tokens,
env, secrets, or raw source paths.

Managed lock namespaces under the shared cache are fail-closed against symlink
escapes: every directory and lock leaf is verified as a real path contained
under the resolved shared-cache root before create/use.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from grok_worker.constants import (
    DISPATCHER_CONCURRENCY_BUSY,
    DISPATCHER_REGISTRY_DIR,
    MAX_CONCURRENT_WORKERS,
)
from grok_worker.locks import FileLock


class DispatcherConcurrencyError(RuntimeError):
    """Structured capacity refusal; never preempts another worker."""

    code = DISPATCHER_CONCURRENCY_BUSY

    def __init__(self, active: int, limit: int, dispatcher_id: str) -> None:
        self.active = active
        self.limit = limit
        self.dispatcher_id = dispatcher_id
        super().__init__(
            f"{DISPATCHER_CONCURRENCY_BUSY}: dispatcher has "
            f"{active} active invocations at limit {limit}; refusing new worker "
            "(no preemption)"
        )


class SameSourceConflictError(RuntimeError):
    """Another implementation invocation already holds the same-source lock."""

    def __init__(self, source_hash: str) -> None:
        self.source_hash = source_hash
        super().__init__(
            "same-source implementation exclusion: another active implementation "
            f"invocation holds source_hash={source_hash!r}; "
            "analysis workers may coexist; reject before clone"
        )


class DispatcherPathError(RuntimeError):
    """Managed dispatcher path is unsafe (symlink or outside shared cache).

    Messages must never include prompts, tokens, env, secrets, or raw source paths.
    """


def _is_reparse_point(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def make_run_id() -> str:
    return uuid.uuid4().hex


def hash_identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _assert_under_cache(path: Path, cache_root: Path) -> None:
    try:
        path.relative_to(cache_root)
    except ValueError as exc:
        raise DispatcherPathError(
            "dispatcher managed path resolves outside shared cache"
        ) from exc


def _safe_path_component(part: str, *, kind: str) -> str:
    if (
        not part
        or part in {".", ".."}
        or "/" in part
        or "\\" in part
        or "\x00" in part
    ):
        raise DispatcherPathError(f"unsafe dispatcher {kind} component")
    return part


def _ensure_managed_dir(cache_root: Path, *parts: str) -> Path:
    """Create/verify each directory leaf under cache; fail closed on symlink escape.

    Returns the resolved real directory path. Never follows a managed leaf that is
    a symlink. Errors omit secrets and absolute attacker destinations.
    """
    configured_root = Path(cache_root)
    if _is_reparse_point(configured_root):
        raise DispatcherPathError("shared cache root is a symlink")
    root = configured_root.resolve()
    current = root
    for part in parts:
        name = _safe_path_component(part, kind="directory")
        candidate = current / name
        if _is_reparse_point(candidate):
            raise DispatcherPathError("dispatcher managed directory is a symlink")
        if not candidate.exists():
            try:
                candidate.mkdir(mode=0o755, exist_ok=True)
            except OSError as exc:
                raise DispatcherPathError("cannot create dispatcher managed directory") from exc
            if _is_reparse_point(candidate):
                raise DispatcherPathError("dispatcher managed directory is a symlink")
        if not candidate.is_dir() or _is_reparse_point(candidate):
            raise DispatcherPathError("dispatcher managed path is not a safe directory")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise DispatcherPathError("cannot resolve dispatcher managed directory") from exc
        if _is_reparse_point(resolved):
            raise DispatcherPathError("dispatcher managed directory is a symlink")
        _assert_under_cache(resolved, root)
        current = resolved
    return current


def _verified_lock_path(parent_dir: Path, name: str, cache_root: Path) -> Path:
    """Return a lock path under a verified parent; refuse symlink leaves.

    The leaf itself is not followed: if it exists as a symlink, fail closed.
    Callers use the returned path with FileLock (which also refuses symlinks).
    """
    configured_root = Path(cache_root)
    if _is_reparse_point(configured_root):
        raise DispatcherPathError("shared cache root is a symlink")
    root = configured_root.resolve()
    lock_name = _safe_path_component(name, kind="lock")
    try:
        parent = parent_dir.resolve(strict=True)
    except OSError as exc:
        raise DispatcherPathError("cannot resolve dispatcher lock parent") from exc
    if _is_reparse_point(parent) or not parent.is_dir():
        raise DispatcherPathError("dispatcher lock parent is not a safe directory")
    _assert_under_cache(parent, root)
    path = parent / lock_name
    if _is_reparse_point(path):
        raise DispatcherPathError("dispatcher lock path is a symlink")
    if path.exists() and not path.is_file():
        raise DispatcherPathError("dispatcher lock path is not a regular file")
    return path


def _safe_dispatcher_dir(shared_cache_root: Path, dispatcher_id: str) -> Path:
    """Return verified dispatchers/<hash>/ under shared cache (hash only — no raw id)."""
    dig = hash_identity(dispatcher_id)
    return _ensure_managed_dir(shared_cache_root, DISPATCHER_REGISTRY_DIR, dig)


def slot_lock_path(shared_cache_root: Path, dispatcher_id: str, index: int) -> Path:
    if index < 0:
        raise ValueError(f"slot index out of range: {index}")
    dig = hash_identity(dispatcher_id)
    slots = _ensure_managed_dir(
        shared_cache_root, DISPATCHER_REGISTRY_DIR, dig, "slots"
    )
    return _verified_lock_path(slots, f"{index:02d}.lock", shared_cache_root)


def source_lock_path(
    shared_cache_root: Path,
    dispatcher_id: str,
    source_hash: str,
) -> Path:
    if not source_hash or "/" in source_hash or "\\" in source_hash or ".." in source_hash:
        raise ValueError("invalid source_hash for lock path")
    dig = hash_identity(dispatcher_id)
    sources = _ensure_managed_dir(
        shared_cache_root, DISPATCHER_REGISTRY_DIR, dig, "sources"
    )
    return _verified_lock_path(sources, f"{source_hash}.lock", shared_cache_root)


def count_held_slots(
    shared_cache_root: Path,
    dispatcher_id: str,
    *,
    limit: int = MAX_CONCURRENT_WORKERS,
) -> int:
    """Best-effort count of currently held slot locks (probe with LOCK_NB).

    Used only for structured error reporting. Capacity enforcement itself is the
    atomic non-blocking acquire of a free slot file.
    """
    held = 0
    if limit < 1:
        raise ValueError("limit must be >= 1")
    for i in range(limit):
        probe = FileLock(slot_lock_path(shared_cache_root, dispatcher_id, i))
        if probe.try_acquire():
            probe.release()
        else:
            held += 1
    return held


def try_acquire_slot(
    shared_cache_root: Path,
    dispatcher_id: str,
    *,
    limit: int = MAX_CONCURRENT_WORKERS,
) -> FileLock:
    """Atomically reserve one of *limit* nonblocking slot locks.

    Raises ``DispatcherConcurrencyError`` with active=limit/limit when all
    slots are held. Never preempts another process.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    n = limit
    for i in range(n):
        lock = FileLock(slot_lock_path(shared_cache_root, dispatcher_id, i))
        if lock.try_acquire():
            return lock
    active = count_held_slots(shared_cache_root, dispatcher_id, limit=n)
    # Report the hard budget (active may briefly lag; never understate limit).
    report_active = max(active, n)
    raise DispatcherConcurrencyError(report_active, n, dispatcher_id)


def try_acquire_source_lock(
    shared_cache_root: Path,
    dispatcher_id: str,
    source_realpath: str,
) -> FileLock:
    """Nonblocking same-source exclusion for implementation mode.

    Lock filename uses only the source hash — never the raw path.
    """
    if not source_realpath or source_realpath in ("", "prompt-only"):
        raise ValueError("source_realpath required for implementation source lock")
    try:
        canonical = str(Path(source_realpath).resolve())
    except OSError:
        canonical = source_realpath
    source_hash = hash_identity(canonical)
    lock = FileLock(source_lock_path(shared_cache_root, dispatcher_id, source_hash))
    if not lock.try_acquire():
        raise SameSourceConflictError(source_hash)
    return lock


@dataclass
class DispatcherLease:
    """Held OS flock leases for one active invocation (slot + optional source).

    Release is idempotent and safe from ``finally``. Process crash drops flock
    automatically.
    """

    slot: FileLock
    source: FileLock | None = None
    dispatcher_id: str = ""
    _released: bool = field(default=False, repr=False)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            if self.source is not None:
                self.source.release()
        finally:
            self.slot.release()

    def __enter__(self) -> DispatcherLease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def reserve_dispatcher_capacity(
    shared_cache_root: Path,
    dispatcher_id: str,
    *,
    mode: str = "analysis",
    source_realpath: str | None = None,
    limit: int = MAX_CONCURRENT_WORKERS,
) -> DispatcherLease:
    """Reserve one active-invocation slot (and source lock when implementation).

    Callers must hold the returned lease for the full active CLI/ACP invocation
    and release it in ``finally`` (or use as a context manager).
    """
    slot = try_acquire_slot(shared_cache_root, dispatcher_id, limit=limit)
    source_lock: FileLock | None = None
    try:
        if mode == "implementation" and source_realpath and source_realpath != "prompt-only":
            source_lock = try_acquire_source_lock(
                shared_cache_root, dispatcher_id, source_realpath
            )
    except Exception:
        slot.release()
        raise
    return DispatcherLease(slot=slot, source=source_lock, dispatcher_id=dispatcher_id)


@contextmanager
def active_dispatcher_invocation(
    shared_cache_root: Path | None,
    dispatcher_id: str | None,
    *,
    mode: str = "analysis",
    source_realpath: str | None = None,
    limit: int = MAX_CONCURRENT_WORKERS,
    disposable_root: Path | None = None,
) -> Iterator[DispatcherLease | None]:
    """Context manager: reserve around a real ACP invocation only.

    - With ``dispatcher_id``: OS flock slot (+ source for implementation).
    - Without ``dispatcher_id`` and with ``disposable_root``: root-scoped
      advisory count of *live* invocations is not performed here; root-scoped
      limits remain caller's responsibility for create-time gates. Returns None.
    - Idle named sessions must not call this while open without an ACP turn.
    """
    if not dispatcher_id or shared_cache_root is None:
        yield None
        return
    lease = reserve_dispatcher_capacity(
        shared_cache_root,
        dispatcher_id,
        mode=mode,
        source_realpath=source_realpath,
        limit=limit,
    )
    try:
        yield lease
    finally:
        lease.release()


# Backward-compatible aliases used by tests that only need error types / hashing.
__all__ = [
    "DispatcherConcurrencyError",
    "DispatcherLease",
    "DispatcherPathError",
    "SameSourceConflictError",
    "active_dispatcher_invocation",
    "count_held_slots",
    "hash_identity",
    "make_run_id",
    "reserve_dispatcher_capacity",
    "slot_lock_path",
    "source_lock_path",
    "try_acquire_slot",
    "try_acquire_source_lock",
]
