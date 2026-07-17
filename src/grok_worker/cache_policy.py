"""Independent shared-cache path, environment, quota, TTL, and LRU policy."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from grok_worker.locks import FileLock
from grok_worker.safety import SafetyError, dir_size_bytes, safe_rmtree, safe_unlink

DEFAULT_CACHE_MAX_BYTES = 10 * 1024**3
DEFAULT_CACHE_TTL_HOURS = 90 * 24
CACHE_BUCKETS = ("context-packs", "venvs", "uv", "pip", "npm", "poetry", "metrics")


class CacheCapacityError(RuntimeError):
    """Shared cache remains over its independent quota after safe GC."""

    def __init__(self, usage: int, limit: int, root: Path) -> None:
        self.usage = usage
        self.limit = limit
        self.root = root
        super().__init__(f"shared cache {root} uses {usage} bytes; limit is {limit}")


@dataclass(frozen=True)
class CachePolicy:
    root: Path
    max_bytes: int = DEFAULT_CACHE_MAX_BYTES
    ttl_hours: float = DEFAULT_CACHE_TTL_HOURS

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("cache max_bytes must be positive")
        if self.ttl_hours <= 0:
            raise ValueError("cache ttl_hours must be positive")


@dataclass
class CacheReport:
    root: str
    max_bytes: int
    before_bytes: int
    after_bytes: int
    removed: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def over_limit(self) -> bool:
        return self.after_bytes > self.max_bytes


@dataclass(frozen=True)
class _Entry:
    key: str
    path: Path
    mtime: float


def _platform_name() -> str:
    return sys.platform


def default_cache_root() -> Path:
    explicit = os.environ.get("GROK_WORKER_CACHE_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return (Path(xdg).expanduser() / "grok-worker").resolve()
    if _platform_name() == "darwin":
        return (Path.home() / "Library" / "Caches" / "grok-worker").resolve()
    if _platform_name() == "win32":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local).expanduser() if local else Path.home() / "AppData" / "Local"
        return (base / "grok-worker" / "Cache").resolve()
    return (Path.home() / ".cache" / "grok-worker").resolve()


def shared_cache_environment(root: Path) -> dict[str, str]:
    root = root.resolve()
    return {
        "UV_CACHE_DIR": str(root / "uv"),
        "PIP_CACHE_DIR": str(root / "pip"),
        "NPM_CONFIG_CACHE": str(root / "npm"),
        "POETRY_CACHE_DIR": str(root / "poetry"),
        "GROK_SHARED_VENV_ROOT": str(root / "venvs"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PIPENV_VENV_IN_PROJECT": "0",
        "POETRY_VIRTUALENVS_IN_PROJECT": "false",
    }


def cache_usage_bytes(root: Path) -> int:
    return dir_size_bytes(root)


def cache_use_lease(root: Path) -> FileLock:
    """Shared lease held for the full period a worker may touch cache entries."""
    return FileLock(root.resolve() / "locks" / "cache-domain.lock", shared=True)


def _cache_gc_lease(root: Path) -> FileLock:
    return FileLock(root.resolve() / "locks" / "cache-domain.lock")


def _entries(root: Path) -> list[_Entry]:
    entries: list[_Entry] = []
    for bucket_name in CACHE_BUCKETS:
        bucket = root / bucket_name
        if not bucket.is_dir() or bucket.is_symlink():
            continue
        for child in bucket.iterdir():
            if child.is_symlink():
                continue
            try:
                entries.append(
                    _Entry(
                        key=f"{bucket_name}/{child.name}",
                        path=child,
                        mtime=child.stat().st_mtime,
                    )
                )
            except OSError:
                continue
    return entries


def _remove(entry: _Entry, root: Path) -> None:
    bucket = entry.path.parent
    protected = [root, Path.home()]
    if entry.path.is_dir():
        safe_rmtree(entry.path, disposable_root=bucket, protected=protected)
    else:
        safe_unlink(entry.path, disposable_root=bucket, protected=protected)


def _evict(
    policy: CachePolicy,
    report: CacheReport,
    protected: set[str] | frozenset[str],
    timestamp: float,
) -> CacheReport:
    root = policy.root.resolve()
    entries = sorted(_entries(root), key=lambda item: (item.mtime, item.key))
    report.protected = sorted(item.key for item in entries if item.key in protected)
    cutoff = timestamp - policy.ttl_hours * 3600
    for entry in entries:
        if entry.key in protected or entry.mtime > cutoff or not entry.path.exists():
            continue
        try:
            _remove(entry, root)
            report.removed.append(entry.key)
        except (OSError, SafetyError) as exc:
            report.errors.append(f"{entry.key}: {exc}")
    usage = cache_usage_bytes(root)
    if usage > policy.max_bytes:
        for entry in entries:
            if usage <= policy.max_bytes:
                break
            if entry.key in protected or not entry.path.exists():
                continue
            try:
                _remove(entry, root)
                if entry.key not in report.removed:
                    report.removed.append(entry.key)
                usage = cache_usage_bytes(root)
            except (OSError, SafetyError) as exc:
                report.errors.append(f"{entry.key}: {exc}")
    report.after_bytes = cache_usage_bytes(root)
    return report


def gc_shared_cache(
    policy: CachePolicy,
    *,
    protected: set[str] | frozenset[str] = frozenset(),
    now: float | None = None,
) -> CacheReport:
    root = policy.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    timestamp = time.time() if now is None else now
    report = CacheReport(
        root=str(root),
        max_bytes=policy.max_bytes,
        before_bytes=cache_usage_bytes(root),
        after_bytes=0,
    )
    lease = _cache_gc_lease(root)
    if not lease.try_acquire():
        report.protected.append("active-cache-users")
        report.after_bytes = report.before_bytes
        return report
    try:
        return _evict(policy, report, protected, timestamp)
    finally:
        lease.release()


def ensure_cache_capacity(
    policy: CachePolicy,
    *,
    protected: set[str] | frozenset[str] = frozenset(),
) -> CacheReport:
    report = gc_shared_cache(policy, protected=protected)
    if report.over_limit:
        raise CacheCapacityError(report.after_bytes, policy.max_bytes, policy.root)
    return report
