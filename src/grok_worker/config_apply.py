"""Transactional config apply with TOML parse, atomic replace, and smoke test.

Never echoes config contents, smoke stdout/stderr, or environment secrets.

Concurrent apply transactions for the same live config path are serialized via
a same-directory exclusive FileLock covering the full
read → backup → replace → smoke → keep/rollback critical section.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grok_worker.locks import FileLock


class ConfigApplyError(ValueError):
    """Config apply refused or rolled back."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _require_regular_file(path: Path, *, label: str, must_exist: bool) -> Path:
    """Require an existing regular non-symlink file when *must_exist* is true.

    Parent symlink chains are not specially policed; only the live/candidate
    path itself must be a regular non-symlink file when present.
    """
    p = Path(path)
    if not p.exists():
        if must_exist:
            raise ConfigApplyError(f"{label} does not exist: {p}")
        return p
    # is_symlink before is_file so a symlink-to-file is refused explicitly.
    if p.is_symlink():
        raise ConfigApplyError(f"{label} must not be a symlink: {p}")
    if not p.is_file():
        raise ConfigApplyError(f"{label} must be a regular file: {p}")
    return p


def _config_apply_lock_path(config_path: Path) -> Path:
    """Exclusive lock beside the live config (same directory as the file)."""
    return config_path.parent / f".{config_path.name}.apply.lock"


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_replace_bytes(target: Path, data: bytes) -> None:
    """Write *data* to *target* via same-dir tempfile + fsync + os.replace."""
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
        _fsync_dir(parent)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _parse_smoke_argv(smoke_argv_json: str) -> list[str]:
    try:
        raw = json.loads(smoke_argv_json)
    except json.JSONDecodeError as exc:
        raise ConfigApplyError(f"smoke-argv-json must be valid JSON: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ConfigApplyError("smoke-argv-json must be a nonempty JSON array")
    argv: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str) or not item:
            raise ConfigApplyError(
                f"smoke-argv-json[{i}] must be a nonempty string"
            )
        argv.append(item)
    return argv


def _validate_smoke_timeout(smoke_timeout: float) -> float:
    """Reject NaN, ±Inf, zero, and negatives before any live-config mutation."""
    try:
        timeout = float(smoke_timeout)
    except (TypeError, ValueError) as exc:
        raise ConfigApplyError("smoke-timeout must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigApplyError("smoke-timeout must be a finite positive number")
    return timeout


def _backup_path(config_path: Path) -> Path:
    return config_path.parent / f"{config_path.name}.bak"


@dataclass
class ConfigApplyReceipt:
    config_path: str
    candidate_path: str
    backup_path: str | None
    original_sha256: str
    candidate_sha256: str
    final_sha256: str
    smoke_exit_code: int | None
    timed_out: bool
    rolled_back: bool
    applied: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_path": self.config_path,
            "candidate_path": self.candidate_path,
            "backup_path": self.backup_path,
            "original_sha256": self.original_sha256,
            "candidate_sha256": self.candidate_sha256,
            "final_sha256": self.final_sha256,
            "smoke_exit_code": self.smoke_exit_code,
            "timed_out": self.timed_out,
            "rolled_back": self.rolled_back,
            "applied": self.applied,
            "error": self.error,
        }


def apply_config(
    *,
    config_path: Path,
    candidate_path: Path,
    smoke_argv_json: str,
    smoke_timeout: float,
) -> tuple[int, ConfigApplyReceipt]:
    """Parse candidate, atomic-apply, smoke-test, rollback on failure.

    Returns (exit_code, receipt). Receipt never contains file contents or smoke
    output. The full critical section for a given live config is serialized.
    """
    cfg = _require_regular_file(config_path, label="config", must_exist=True)
    cand = _require_regular_file(candidate_path, label="candidate", must_exist=True)

    # Reject bad timeouts before reading or mutating the live config.
    timeout = _validate_smoke_timeout(smoke_timeout)
    argv = _parse_smoke_argv(smoke_argv_json)

    try:
        candidate_bytes = cand.read_bytes()
    except OSError as exc:
        raise ConfigApplyError(f"cannot read candidate: {exc}") from exc
    candidate_hash = _sha256_bytes(candidate_bytes)

    # Parse candidate as TOML before any mutation of live config.
    try:
        tomllib.loads(candidate_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        try:
            original_hash = _sha256_path(cfg)
        except OSError:
            original_hash = ""
        receipt = ConfigApplyReceipt(
            config_path=str(cfg),
            candidate_path=str(cand),
            backup_path=None,
            original_sha256=original_hash,
            candidate_sha256=candidate_hash,
            final_sha256=original_hash,
            smoke_exit_code=None,
            timed_out=False,
            rolled_back=False,
            applied=False,
            error="invalid_toml_candidate",
        )
        return 1, receipt

    lock = FileLock(_config_apply_lock_path(cfg))
    with lock:
        try:
            original_bytes = cfg.read_bytes()
        except OSError as exc:
            raise ConfigApplyError(f"cannot read config: {exc}") from exc
        original_hash = _sha256_bytes(original_bytes)

        backup = _backup_path(cfg)
        _atomic_replace_bytes(backup, original_bytes)

        # Apply candidate.
        _atomic_replace_bytes(cfg, candidate_bytes)

        timed_out = False
        smoke_exit: int | None = None
        try:
            proc = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            smoke_exit = int(proc.returncode)
            # Intentionally discard stdout/stderr; never echo.
            _ = proc.stdout
            _ = proc.stderr
        except subprocess.TimeoutExpired:
            timed_out = True
            smoke_exit = None
        except OSError:
            # Treat launch failure like a failed smoke (rollback).
            smoke_exit = 127

        if timed_out or smoke_exit is None or smoke_exit != 0:
            # Exact byte-level rollback of the original config.
            _atomic_replace_bytes(cfg, original_bytes)
            final_hash = _sha256_path(cfg)
            receipt = ConfigApplyReceipt(
                config_path=str(cfg),
                candidate_path=str(cand),
                backup_path=str(backup),
                original_sha256=original_hash,
                candidate_sha256=candidate_hash,
                final_sha256=final_hash,
                smoke_exit_code=smoke_exit,
                timed_out=timed_out,
                rolled_back=True,
                applied=False,
                error="smoke_timeout" if timed_out else "smoke_failed",
            )
            return 1, receipt

        final_hash = _sha256_path(cfg)
        receipt = ConfigApplyReceipt(
            config_path=str(cfg),
            candidate_path=str(cand),
            backup_path=str(backup),
            original_sha256=original_hash,
            candidate_sha256=candidate_hash,
            final_sha256=final_hash,
            smoke_exit_code=smoke_exit,
            timed_out=False,
            rolled_back=False,
            applied=True,
            error=None,
        )
        return 0, receipt
