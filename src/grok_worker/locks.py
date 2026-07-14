"""File-based exclusive locks for root, worker, and shared env preparation."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from grok_worker.process_identity import process_matches

if sys.platform == "win32":
    fcntl = None
else:
    import fcntl

# Shared acquire polls with LOCK_NB instead of blocking LOCK_SH.
# On Darwin, blocked flock(LOCK_SH) waiters are woken one-at-a-time (exclusive
# FIFO) after any exclusive holder, which serializes concurrent shared
# cache-domain leases once ensure_cache_capacity (EX|NB) races with workers.
# Non-blocking polls let all waiters enter together once EX is released.
_SHARED_LOCK_POLL_S = 0.01


@dataclass
class FileLock:
    path: Path
    shared: bool = False
    _fd: int | None = None

    @staticmethod
    def _require_posix_locking() -> None:
        if fcntl is None:
            raise RuntimeError(
                "grok-worker requires POSIX flock semantics; native Windows is not yet supported"
            )

    def acquire(self) -> None:
        self._require_posix_locking()
        assert fcntl is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if self.shared:
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(_SHARED_LOCK_POLL_S)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

    def try_acquire(self) -> bool:
        """Non-blocking acquire. True if held by caller."""
        self._require_posix_locking()
        assert fcntl is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            operation = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        assert fcntl is not None
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def root_lock(disposable_root: Path) -> FileLock:
    from grok_worker.constants import ROOT_LOCK_NAME

    return FileLock(disposable_root / ROOT_LOCK_NAME)


def worker_lock(meta_dir: Path) -> FileLock:
    from grok_worker.constants import WORKER_LOCK_NAME

    return FileLock(meta_dir / WORKER_LOCK_NAME)


def fingerprint_lock(shared_root: Path, fingerprint: str) -> FileLock:
    return FileLock(shared_root / "locks" / f"{fingerprint}.lock")


def pid_is_alive(pid: int | None, start_token: str | None = None) -> bool:
    """Liveness with birth identity. Without a token, never claim alive."""
    return process_matches(pid, start_token)
