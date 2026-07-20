"""Cross-platform detached launcher for the existing one-shot lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, BinaryIO

from grok_worker.dispatcher import hash_identity
from grok_worker.paths import default_shared_cache_root
from grok_worker.run_config import RunConfig

_PATH_FIELDS = frozenset(
    {"source", "disposable_root", "artifact_root", "shared_cache_root"}
)
_DETACHED_CHILDREN: list[subprocess.Popen[bytes]] = []


class DetachedStartError(RuntimeError):
    """The detached child could not be created or receive its run config."""


def run_config_to_payload(cfg: RunConfig) -> dict[str, Any]:
    payload = asdict(cfg)
    for name in _PATH_FIELDS:
        value = payload[name]
        payload[name] = str(value) if value is not None else None
    return payload


def run_config_from_payload(payload: object) -> RunConfig:
    if not isinstance(payload, dict):
        raise ValueError("detached run config must be a JSON object")
    allowed = {item.name for item in fields(RunConfig)}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError("detached run config contains unknown fields")
    values = dict(payload)
    for name in _PATH_FIELDS:
        value = values.get(name)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"detached run config field {name} must be a path string")
            values[name] = Path(value)
    return RunConfig(**values)


def _safe_launch_log(shared_cache_root: Path, run_id: str) -> tuple[BinaryIO, Path]:
    root = shared_cache_root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    launch_dir = root / "launch-logs"
    if launch_dir.is_symlink():
        raise DetachedStartError("detached launch-log directory must not be a symlink")
    launch_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = launch_dir.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DetachedStartError("detached launch-log directory escaped shared cache") from exc
    prefix = f"{hash_identity(run_id)}-"
    fd, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".log", dir=resolved)
    os.chmod(raw_path, 0o600)
    return os.fdopen(fd, "wb"), Path(raw_path)


def _child_command() -> list[str]:
    return [sys.executable, "-m", "grok_worker", "_run-detached"]


def _child_environment() -> dict[str, str]:
    env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_root if not existing else source_root + os.pathsep + existing
    return env


def _reap_finished_children() -> None:
    _DETACHED_CHILDREN[:] = [proc for proc in _DETACHED_CHILDREN if proc.poll() is None]


def start_detached_run(cfg: RunConfig) -> dict[str, Any]:
    """Start the normal lifecycle in a new process and return an observation receipt."""
    if not cfg.run_id:
        raise DetachedStartError("detached run requires a stable run_id")
    shared = (cfg.shared_cache_root or default_shared_cache_root()).resolve()
    log_stream, log_path = _safe_launch_log(shared, cfg.run_id)
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": log_stream,
        "stderr": subprocess.STDOUT,
        "cwd": str(Path.cwd()),
        "env": _child_environment(),
        "close_fds": True,
    }
    if os.name == "nt":
        create_group = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        detached_process = int(getattr(subprocess, "DETACHED_PROCESS", 0))
        popen_kwargs["creationflags"] = create_group | detached_process
    else:
        popen_kwargs["start_new_session"] = True

    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(_child_command(), **popen_kwargs)
        if process.stdin is None:
            raise DetachedStartError("detached worker stdin pipe was not created")
        encoded = (json.dumps(run_config_to_payload(cfg), separators=(",", ":")) + "\n").encode()
        process.stdin.write(encoded)
        process.stdin.close()
    except (OSError, BrokenPipeError, ValueError, DetachedStartError) as exc:
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
        raise DetachedStartError(f"could not start detached worker: {exc}") from exc
    finally:
        log_stream.close()

    assert process is not None
    _reap_finished_children()
    _DETACHED_CHILDREN.append(process)
    return {
        "accepted": True,
        "task_id": cfg.task_id,
        "run_id": cfg.run_id,
        "dispatcher_id": cfg.dispatcher_id,
        "pid": process.pid,
        "backend": cfg.backend,
        "mode": cfg.mode,
        "shared_cache_root": str(shared),
        "disposable_root": str(cfg.disposable_root) if cfg.disposable_root else None,
        "artifact_root": str(cfg.artifact_root) if cfg.artifact_root else None,
        "launch_log": str(log_path),
        "watch_wait_seconds": 300,
    }


__all__ = [
    "DetachedStartError",
    "run_config_from_payload",
    "run_config_to_payload",
    "start_detached_run",
]
