"""Stable process identity (PID + birth token) for GC liveness checks."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def windows_descendant_pids(root_pid: int) -> list[int]:
    """Snapshot live Windows descendants, including children of an exited root."""
    if sys.platform != "win32" or root_pid <= 0:
        return []

    import ctypes
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    relationships: list[tuple[int, int]] = []
    entry = ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        if not process_first(snapshot, ctypes.byref(entry)):
            return []
        while True:
            relationships.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            if not process_next(snapshot, ctypes.byref(entry)):
                break
    finally:
        close_handle(snapshot)

    descendants: list[int] = []
    frontier = {root_pid}
    seen = {root_pid}
    while frontier:
        children = {
            pid for pid, parent_pid in relationships if parent_pid in frontier and pid not in seen
        }
        if not children:
            break
        descendants.extend(sorted(children))
        seen.update(children)
        frontier = children
    return descendants


def _windows_process_start_token(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    get_process_times.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        return None
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel_time = wintypes.FILETIME()
    user_time = wintypes.FILETIME()
    try:
        if not get_process_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        return f"winfiletime:{value}"
    finally:
        close_handle(handle)


def process_start_token(pid: int) -> str | None:
    """Return a stable start token for *pid*, or None if unavailable."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _windows_process_start_token(pid)
    # Prefer /proc on Linux
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            # Field 22 is starttime (clock ticks after boot)
            data = stat_path.read_text(encoding="utf-8", errors="replace")
            # comm may contain spaces/parens; split after last ')'
            rparen = data.rfind(")")
            if rparen != -1:
                fields = data[rparen + 2 :].split()
                if len(fields) >= 20:
                    return f"proc:{fields[19]}"
        except OSError:
            pass
    try:
        out = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return out or None
    except (OSError, subprocess.CalledProcessError):
        return None


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_start_token(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_matches(pid: int | None, start_token: str | None) -> bool:
    """True only when pid is live *and* birth identity matches.

    Missing start_token means identity cannot be verified → not a match.
    """
    if pid is None or pid <= 0:
        return False
    if not start_token:
        return False
    if not pid_exists(pid):
        return False
    current = process_start_token(pid)
    if current is None:
        return False
    return current == start_token


def capture_identity(pid: int | None = None) -> tuple[int | None, str | None]:
    """Capture (pid, start_token) for the given or current process."""
    p = os.getpid() if pid is None else pid
    if p is None or p <= 0:
        return None, None
    return p, process_start_token(p)
