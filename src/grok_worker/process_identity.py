"""Stable process identity (PID + birth token) for GC liveness checks."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path


def windows_descendant_pids(root_pid: int) -> list[int]:
    """Snapshot live Windows descendants, including children of an exited root."""
    if sys.platform != "win32" or root_pid <= 0:
        return []

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
            relationships.append(
                (int(entry.th32ProcessID), int(entry.th32ParentProcessID))
            )
            if not process_next(snapshot, ctypes.byref(entry)):
                break
    finally:
        close_handle(snapshot)

    descendants: list[int] = []
    frontier = {root_pid}
    seen = {root_pid}
    while frontier:
        children = {
            pid
            for pid, parent_pid in relationships
            if parent_pid in frontier and pid not in seen
        }
        if not children:
            break
        descendants.extend(sorted(children))
        seen.update(children)
        frontier = children
    return descendants


def _windows_process_start_token(pid: int) -> str | None:
    from ctypes import wintypes

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

    handle = open_process(0x1000, False, pid)
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


def _darwin_start_token(pid: int) -> str | None:
    if sys.platform != "darwin":
        return None

    class ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        info = ProcBsdInfo()
        size = ctypes.sizeof(info)
        written = proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
    except (AttributeError, OSError):
        return None
    if written != size or info.pbi_start_tvsec <= 0:
        return None
    return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"


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
    darwin = _darwin_start_token(pid)
    if darwin is not None:
        return darwin
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

    A sandbox may deny the macOS ``ps`` birth-token probe. In that case only
    the current process may match itself; every other PID remains fail-closed.
    """
    if pid is None or pid <= 0:
        return False
    if not start_token:
        return pid == os.getpid() and pid_exists(pid)
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
