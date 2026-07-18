"""Typed lifecycle metadata and structured result models."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from grok_worker.constants import MANAGED_BY, SCHEMA_VERSION


class WorkerState(StrEnum):
    CREATING = "creating"
    RUNNING = "running"
    FINALIZING = "finalizing"
    SUCCESS = "success"
    FAILED = "failed"
    KEEP = "keep"
    LEGACY_IMPORTED = "legacy_imported"
    SESSION_OPEN = "session_open"


class LegacyClass(StrEnum):
    KEEP = "keep"
    EXPIRE = "expire"
    RETAIN_24H = "retain-24h"


class ResultStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


def utc_now() -> datetime:
    return datetime.now(UTC)


def dt_to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def dt_from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def atomic_write_text(path: Path, text: str) -> None:
    """Write via tempfile in the same directory then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".lifecycle-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@dataclass
class WorkerMeta:
    schema_version: int
    task_id: str
    source_realpath: str
    clone_realpath: str
    state: WorkerState
    created_at: str
    updated_at: str
    managed_by: str = MANAGED_BY
    base_commit: str | None = None
    runner_pid: int | None = None
    runner_start_token: str | None = None
    acpx_pid: int | None = None
    acpx_start_token: str | None = None
    # Legacy alias field name kept for older tests; prefer runner_pid
    pid: int | None = None
    keep_reason: str | None = None
    retention_deadline: str | None = None
    artifact_path: str | None = None
    artifact_complete: bool = False
    exit_code: int | None = None
    result_status: str | None = None
    acpx_exit_code: int | None = None
    error_message: str | None = None
    legacy_classification: str | None = None
    source_state_fingerprint: str | None = None
    interrupted: bool = False
    # Optional timeout recorded at lifecycle creation for status remaining_seconds.
    # Absent on older metadata → status reports null (backward compatible).
    timeout_seconds: int | None = None
    # Unique per execution; used for completion-event dedup with state.
    run_id: str | None = None
    # Explicit dispatcher scope for cross-root concurrency (optional).
    dispatcher_id: str | None = None
    # analysis | implementation | prompt-only research marker
    mode: str | None = None
    # Structured disclosure summary (values/content/prompt/env-free). Survives
    # successful clone deletion because lifecycle is copied into worker.log.
    disclosure_summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = str(self.state)
        return data

    def write(self, path: Path) -> None:
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        atomic_write_text(path, payload)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerMeta:
        return cls(
            schema_version=int(data["schema_version"]),
            task_id=str(data["task_id"]),
            source_realpath=str(data["source_realpath"]),
            clone_realpath=str(data["clone_realpath"]),
            state=WorkerState(str(data["state"])),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            managed_by=str(data.get("managed_by", "")),
            base_commit=data.get("base_commit"),
            runner_pid=data.get("runner_pid", data.get("pid")),
            runner_start_token=data.get("runner_start_token"),
            acpx_pid=data.get("acpx_pid"),
            acpx_start_token=data.get("acpx_start_token"),
            pid=data.get("pid", data.get("runner_pid")),
            keep_reason=data.get("keep_reason"),
            retention_deadline=data.get("retention_deadline"),
            artifact_path=data.get("artifact_path"),
            artifact_complete=bool(data.get("artifact_complete", False)),
            exit_code=data.get("exit_code"),
            result_status=data.get("result_status"),
            acpx_exit_code=data.get("acpx_exit_code"),
            error_message=data.get("error_message"),
            legacy_classification=data.get("legacy_classification"),
            source_state_fingerprint=data.get("source_state_fingerprint"),
            interrupted=bool(data.get("interrupted", False)),
            timeout_seconds=_optional_int(data.get("timeout_seconds")),
            run_id=_optional_str(data.get("run_id")),
            dispatcher_id=_optional_str(data.get("dispatcher_id")),
            mode=_optional_str(data.get("mode")),
            disclosure_summary=_optional_disclosure(data.get("disclosure_summary")),
        )

    @classmethod
    def read(cls, path: Path) -> WorkerMeta:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"invalid metadata at {path}")
        return cls.from_dict(data)

    def touch(self, state: WorkerState | None = None) -> None:
        if state is not None:
            self.state = state
        self.updated_at = dt_to_iso(utc_now()) or ""


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value else None
    return None


def _optional_disclosure(value: Any) -> dict[str, Any] | None:
    """Accept only a plain dict of scalar/list summary fields (no nested secrets)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    # Shallow copy; never invent content. Callers must pass values/content-free summaries.
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if item is None or isinstance(item, (str, int, float, bool)):
            out[key] = item
        elif isinstance(item, list):
            # Only lists of scalars/strings (e.g. included_dirty_paths, reason_codes).
            cleaned: list[Any] = []
            for el in item:
                if el is None or isinstance(el, (str, int, float, bool)):
                    cleaned.append(el)
            out[key] = cleaned
        elif isinstance(item, dict):
            # Nested plain maps of scalars only (defensive).
            nested: dict[str, Any] = {}
            for nk, nv in item.items():
                if isinstance(nk, str) and (
                    nv is None or isinstance(nv, (str, int, float, bool))
                ):
                    nested[nk] = nv
            out[key] = nested
    return out if out else None


def meta_is_trusted(meta: WorkerMeta) -> bool:
    """GC may only act on trusted, known-schema managed metadata."""
    if meta.managed_by != MANAGED_BY:
        return False
    if meta.schema_version != SCHEMA_VERSION:
        return False
    return True


@dataclass
class VerificationRecord:
    command: str
    exit_code: int
    log_path: str


@dataclass
class WorkerResult:
    schema_version: int
    task_completed: bool
    status: ResultStatus
    summary: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    verification: list[VerificationRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_completed": self.task_completed,
            "status": str(self.status),
            "summary": self.summary,
            "findings": self.findings,
            "verification": [
                {
                    "command": v.command,
                    "exit_code": v.exit_code,
                    "log_path": v.log_path,
                }
                for v in self.verification
            ],
        }
