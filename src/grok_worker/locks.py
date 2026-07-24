"""Cross-platform file locks for lifecycle and shared-cache coordination."""

from __future__ import annotations

import errno
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from grok_worker.process_identity import process_matches

# All blocking acquires poll the platform's non-blocking primitive. On Darwin,
# blocked flock(LOCK_SH) waiters otherwise wake one-at-a-time after an exclusive
# holder. On Windows, polling avoids msvcrt's bounded ten-second blocking modes.
_SHARED_LOCK_POLL_S = 0.01
_WINDOWS_LOCK_BYTES = 1
_WINDOWS_LOCK_OFFSET = 0x7FFF_FFFF


def _open_lock_file(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("refusing symlink lock path")
    flags = os.O_RDWR | os.O_CREAT
    if sys.platform == "win32":
        flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(str(path), flags, 0o644)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EEXIST} or path.is_symlink():
            raise RuntimeError("refusing symlink lock path") from exc
        raise


def _try_lock_fd(fd: int, *, shared: bool) -> None:
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        lock_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).LockFileEx
        lock_file_ex.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        lock_file_ex.restype = wintypes.BOOL
        lockfile_fail_immediately = 0x00000001
        lockfile_exclusive_lock = 0x00000002
        flags = lockfile_fail_immediately
        if not shared:
            flags |= lockfile_exclusive_lock
        overlapped = Overlapped(Offset=_WINDOWS_LOCK_OFFSET)
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        if not lock_file_ex(handle, flags, 0, _WINDOWS_LOCK_BYTES, 0, ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if error == 33:  # ERROR_LOCK_VIOLATION
                raise BlockingIOError(errno.EACCES, "lock is held by another process")
            raise OSError(error, ctypes.FormatError(error))
        return

    import fcntl

    operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    fcntl.flock(fd, operation | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        unlock_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).UnlockFileEx
        unlock_file_ex.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        unlock_file_ex.restype = wintypes.BOOL
        overlapped = Overlapped(Offset=_WINDOWS_LOCK_OFFSET)
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        if not unlock_file_ex(handle, 0, _WINDOWS_LOCK_BYTES, 0, ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error))
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@dataclass
class FileLock:
    path: Path
    shared: bool = False
    _fd: int | None = None

    def acquire(self) -> None:
        fd = _open_lock_file(self.path)
        try:
            while True:
                try:
                    _try_lock_fd(fd, shared=self.shared)
                    break
                except BlockingIOError:
                    time.sleep(_SHARED_LOCK_POLL_S)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

    def try_acquire(self) -> bool:
        """Non-blocking acquire. True if held by caller."""
        fd = _open_lock_file(self.path)
        try:
            _try_lock_fd(fd, shared=self.shared)
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
        try:
            _unlock_fd(self._fd)
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

    if sys.platform == "win32":
        clone = meta_dir.parent
        return FileLock(clone.parent / ".grok-worker-locks" / f"{clone.name}.{WORKER_LOCK_NAME}")
    return FileLock(meta_dir / WORKER_LOCK_NAME)


def fingerprint_lock(shared_root: Path, fingerprint: str) -> FileLock:
    return FileLock(shared_root / "locks" / f"{fingerprint}.lock")


def pid_is_alive(pid: int | None, start_token: str | None = None) -> bool:
    """Liveness with birth identity. Without a token, never claim alive."""
    return process_matches(pid, start_token)
