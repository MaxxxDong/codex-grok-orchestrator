"""Semaphore-free subprocess harness for real cross-process lock tests."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _worker_command(action: str, ready: Path, go: Path, *args: object) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        action,
        str(ready),
        str(go),
        *(str(arg) for arg in args),
    ]


def _stop_processes(processes: list[subprocess.Popen[str]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def wait_for_path(path: Path, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and not path.is_symlink():
            return True
        time.sleep(0.01)
    return False


def run_barrier_workers(
    tmp_path: Path,
    action: str,
    arguments: list[tuple[object, ...]],
    *,
    timeout: float = 30.0,
) -> tuple[list[dict[str, Any]], float]:
    """Start real processes together using files instead of multiprocessing SemLock."""
    sync_root = tmp_path / f"{action}-sync"
    ready_root = sync_root / "ready"
    ready_root.mkdir(parents=True)
    go = sync_root / "go"
    processes: list[subprocess.Popen[str]] = []
    started = time.monotonic()
    try:
        for index, args in enumerate(arguments):
            ready = ready_root / f"{index}.ready"
            process = subprocess.Popen(
                _worker_command(action, ready, go, *args),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(process)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready_count = sum(path.is_file() for path in ready_root.glob("*.ready"))
            if ready_count == len(processes):
                break
            failed = [process for process in processes if process.poll() is not None]
            if failed:
                details = [
                    {
                        "returncode": process.returncode,
                        "stdout": process.stdout.read() if process.stdout else "",
                        "stderr": process.stderr.read() if process.stderr else "",
                    }
                    for process in failed
                ]
                raise AssertionError(f"worker exited before barrier: {details}")
            time.sleep(0.01)
        else:
            raise AssertionError("workers did not reach file barrier")

        go.write_text("go\n", encoding="utf-8")
        results: list[dict[str, Any]] = []
        for process in processes:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise AssertionError(f"worker stuck pid={process.pid}") from exc
            if process.returncode != 0:
                raise AssertionError(
                    f"worker failed pid={process.pid} exit={process.returncode}: {stderr}"
                )
            try:
                result = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise AssertionError(f"worker returned invalid JSON: {stdout!r}") from exc
            if not isinstance(result, dict):
                raise AssertionError(f"worker result must be an object: {result!r}")
            results.append(result)
        return results, time.monotonic() - started
    finally:
        _stop_processes(processes)


def start_crash_holder(shared: Path, dispatcher_id: str, ready: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        _worker_command("crash-holder", ready, ready.parent / "unused-go", shared, dispatcher_id),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_go(ready: Path, go: Path) -> None:
    ready.parent.mkdir(parents=True, exist_ok=True)
    ready.write_text("ready\n", encoding="utf-8")
    if not wait_for_path(go, timeout=30):
        raise TimeoutError("file barrier timed out")


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def _run_worker(action: str, ready: Path, go: Path, args: list[str]) -> None:
    if action == "crash-holder":
        from grok_worker.dispatcher import try_acquire_slot

        shared, dispatcher_id = args
        lock = try_acquire_slot(Path(shared), dispatcher_id, limit=10)
        ready.parent.mkdir(parents=True, exist_ok=True)
        ready.write_text("ready\n", encoding="utf-8")
        time.sleep(60)
        _ = lock
        return

    _wait_for_go(ready, go)
    if action == "ex-then-sh":
        from grok_worker.locks import FileLock

        index, path, hold_s = args
        ex = FileLock(Path(path), shared=False)
        if ex.try_acquire():
            time.sleep(0.05)
            ex.release()
        shared = FileLock(Path(path), shared=True)
        started = time.time()
        shared.acquire()
        acquired = time.time()
        time.sleep(float(hold_s))
        released = time.time()
        shared.release()
        _emit(
            {
                "i": int(index),
                "wait_ms": (acquired - started) * 1000,
                "got": acquired,
                "rel": released,
            }
        )
        return

    if action == "ensure-then-sh":
        from grok_worker.cache_policy import (
            CachePolicy,
            cache_use_lease,
            ensure_cache_capacity,
        )

        index, root, hold_s = args
        root_path = Path(root)
        ensure_cache_capacity(CachePolicy(root=root_path, max_bytes=10**12, ttl_hours=720))
        lease = cache_use_lease(root_path)
        started = time.time()
        lease.acquire()
        acquired = time.time()
        time.sleep(float(hold_s))
        released = time.time()
        lease.release()
        _emit(
            {
                "i": int(index),
                "wait_ms": (acquired - started) * 1000,
                "got": acquired,
                "rel": released,
            }
        )
        return

    if action == "slot":
        from grok_worker.dispatcher import DispatcherConcurrencyError, try_acquire_slot

        index, shared, dispatcher_id, hold_s = args
        try:
            lock = try_acquire_slot(Path(shared), dispatcher_id)
        except DispatcherConcurrencyError as exc:
            _emit({"i": int(index), "ok": False, "code": exc.code, "active": exc.active})
            return
        started = time.time()
        time.sleep(float(hold_s))
        lock.release()
        _emit({"i": int(index), "ok": True, "held_s": time.time() - started})
        return

    if action == "implementation-source":
        from grok_worker.dispatcher import (
            SameSourceConflictError,
            reserve_dispatcher_capacity,
        )

        index, shared, dispatcher_id, source, hold_s = args
        try:
            lease = reserve_dispatcher_capacity(
                Path(shared),
                dispatcher_id,
                mode="implementation",
                source_realpath=source,
            )
        except SameSourceConflictError as exc:
            _emit({"i": int(index), "ok": False, "err": "source", "hash": exc.source_hash})
            return
        except Exception as exc:  # noqa: BLE001
            _emit({"i": int(index), "ok": False, "err": type(exc).__name__})
            return
        time.sleep(float(hold_s))
        lease.release()
        _emit({"i": int(index), "ok": True})
        return

    raise ValueError(f"unknown worker action: {action}")


if __name__ == "__main__":
    worker_action, ready_arg, go_arg, *worker_args = sys.argv[1:]
    _run_worker(worker_action, Path(ready_arg), Path(go_arg), worker_args)
