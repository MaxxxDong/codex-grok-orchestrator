"""Strict validation for .grok-output/result.json and verification logs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from grok_worker.constants import (
    OUTPUT_DIR_NAME,
    RESULT_FILE_NAME,
    RESULT_SCHEMA_VERSION,
    VERIFICATION_DIR_NAME,
)
from grok_worker.models import (
    ResultStatus,
    VerificationRecord,
    WorkerResult,
    atomic_write_text,
)


class ResultError(ValueError):
    """Invalid or missing structured worker result."""


def result_path(clone: Path) -> Path:
    return clone / OUTPUT_DIR_NAME / RESULT_FILE_NAME


def _assert_contained_no_symlinks(clone: Path, rel: Path) -> Path:
    """Ensure every component from clone through *rel* is non-symlink and inside clone."""
    if rel.is_absolute() or ".." in rel.parts:
        raise ResultError(f"path must be relative without '..': {rel}")
    try:
        clone_res = clone.resolve()
    except OSError as exc:
        raise ResultError(f"cannot resolve clone: {exc}") from exc
    cur = clone
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ResultError(f"refusing symlink path component: {cur.relative_to(clone)}")
    try:
        resolved = cur.resolve()
        resolved.relative_to(clone_res)
    except (OSError, ValueError) as exc:
        raise ResultError(f"path escapes clone: {rel}") from exc
    return cur


def _safe_rel_log_path(log_path: str) -> Path:
    """Require log_path under .grok-output/verification/ with no traversal."""
    if not log_path or not isinstance(log_path, str):
        raise ResultError("verification.log_path must be a nonempty string")
    if log_path.startswith("/") or log_path.startswith("\\"):
        raise ResultError(f"verification.log_path must be relative: {log_path}")
    p = Path(log_path)
    if ".." in p.parts:
        raise ResultError(f"verification.log_path must not contain '..': {log_path}")
    expected_prefix = (OUTPUT_DIR_NAME, VERIFICATION_DIR_NAME)
    if p.parts[:2] != expected_prefix:
        raise ResultError(
            f"verification.log_path must be under {OUTPUT_DIR_NAME}/{VERIFICATION_DIR_NAME}/: "
            f"{log_path}"
        )
    return p


def _parse_verification(raw: object) -> list[VerificationRecord]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ResultError("verification must be a list")
    out: list[VerificationRecord] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ResultError(f"verification[{i}] must be an object")
        for key in ("command", "exit_code", "log_path"):
            if key not in item:
                raise ResultError(f"verification[{i}] missing {key}")
        cmd = item["command"]
        if not isinstance(cmd, str) or not cmd.strip():
            raise ResultError(f"verification[{i}].command must be nonempty string")
        exit_code = item["exit_code"]
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ResultError(f"verification[{i}].exit_code must be an integer")
        log_path = item["log_path"]
        if not isinstance(log_path, str):
            raise ResultError(f"verification[{i}].log_path must be a string")
        _safe_rel_log_path(log_path)
        out.append(VerificationRecord(command=cmd.strip(), exit_code=exit_code, log_path=log_path))
    return out


def parse_result_dict(data: dict[str, object]) -> WorkerResult:
    required = {"schema_version", "task_completed", "status", "summary", "verification"}
    missing = required - set(data)
    if missing:
        raise ResultError(f"result.json missing keys: {sorted(missing)}")
    if not isinstance(data["task_completed"], bool):
        raise ResultError("task_completed must be a boolean")
    status = ResultStatus(str(data["status"]))
    summary = str(data["summary"])
    if not summary.strip():
        raise ResultError("result.summary must be nonempty")
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        raise ResultError("findings must be a list")
    verification = _parse_verification(data["verification"])
    sv = data["schema_version"]
    if not isinstance(sv, int) or isinstance(sv, bool):
        raise ResultError("schema_version must be an integer")
    findings_list: list[dict[str, object]] = []
    for item in findings:
        if not isinstance(item, dict):
            raise ResultError("findings entries must be objects")
        findings_list.append(dict(item))
    return WorkerResult(
        schema_version=sv,
        task_completed=bool(data["task_completed"]),
        status=status,
        summary=summary,
        findings=cast(list[dict[str, Any]], findings_list),
        verification=verification,
    )


def load_valid_result(clone: Path) -> WorkerResult:
    rel = Path(OUTPUT_DIR_NAME) / RESULT_FILE_NAME
    path = _assert_contained_no_symlinks(clone, rel)
    if not path.is_file() or path.is_symlink():
        raise ResultError(f"missing structured result: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResultError(f"invalid structured result: {exc}") from exc
    if not isinstance(raw, dict):
        raise ResultError("result.json must be an object")
    result = parse_result_dict(raw)
    if result.schema_version != RESULT_SCHEMA_VERSION:
        raise ResultError(
            f"unsupported result schema_version {result.schema_version}; "
            f"expected {RESULT_SCHEMA_VERSION}"
        )
    return result


def write_captured_analysis_result(clone: Path, agent_log: Path | None) -> bool:
    """Root-owned result for a read-only analysis whose nonempty ACP log is the output."""
    if agent_log is None or not agent_log.is_file() or agent_log.is_symlink():
        return False
    response = agent_log.read_text(encoding="utf-8", errors="replace").strip()
    if not response:
        return False
    output = clone / OUTPUT_DIR_NAME
    if output.is_symlink():
        raise ResultError(f"refusing symlink output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    path = result_path(clone)
    if path.exists() or path.is_symlink():
        return False
    payload = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "task_completed": True,
        "status": "completed",
        "summary": "Read-only analysis response captured in worker.log",
        "findings": [],
        "verification": [],
    }
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return True


def validate_verification_files(clone: Path, result: WorkerResult) -> None:
    """Ensure every verification log is a regular file with no symlink components."""
    for i, rec in enumerate(result.verification):
        rel = _safe_rel_log_path(rec.log_path)
        try:
            fp = _assert_contained_no_symlinks(clone, rel)
        except ResultError as exc:
            raise ResultError(f"verification[{i}] {exc}") from exc
        if fp.is_symlink():
            raise ResultError(f"verification[{i}] log is a symlink: {rec.log_path}")
        if not fp.is_file():
            raise ResultError(f"verification[{i}] log missing: {rec.log_path}")


def is_task_success(
    acpx_exit: int,
    result: WorkerResult | None,
    *,
    mode: str = "implementation",
) -> bool:
    """Semantic success: acpx 0, task_completed, status=completed only.

    Implementation mode requires ≥1 verification record with exit_code 0 and
    no nonzero verification exits. Analysis mode may use an empty verification list.
    """
    if acpx_exit != 0:
        return False
    if result is None:
        return False
    if not result.task_completed:
        return False
    if result.status != ResultStatus.COMPLETED:
        return False
    if any(v.exit_code != 0 for v in result.verification):
        return False
    if mode == "implementation" and not result.verification:
        return False
    if mode == "implementation" and not any(v.exit_code == 0 for v in result.verification):
        return False
    return True
