"""Collect a patch and embed all audit material in exactly three files."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from grok_worker.artifact_contract import (
    ContractError,
    clone_deletion_authorized,
    write_artifact_contract,
)
from grok_worker.artifact_contract import (
    verify_artifact_contract as _verify_v2,
)
from grok_worker.artifact_legacy import (
    LegacyArtifactError,
    sha256_file,
)
from grok_worker.artifact_legacy import (
    verify_manifest as _verify_v1,
)
from grok_worker.artifact_legacy import (
    write_manifest as _write_manifest,
)
from grok_worker.constants import OUTPUT_DIR_NAME, STAGING_PREFIX, VERIFICATION_DIR_NAME
from grok_worker.models import WorkerMeta, WorkerState
from grok_worker.patch_capture import PatchError, collect_git_patch
from grok_worker.paths import artifact_outside_clone
from grok_worker.result_schema import result_path


class ArtifactError(RuntimeError):
    """Artifact finalization or verification failed."""


def write_manifest(root: Path) -> Path:
    """Write a v1 manifest only for retained-v1 compatibility tests/tools."""
    return _write_manifest(root)


def verify_artifact_contract(root: Path) -> None:
    try:
        _verify_v2(root)
    except ContractError as exc:
        raise ArtifactError(str(exc)) from exc


def artifact_authorizes_clone_deletion(root: Path) -> bool:
    try:
        return clone_deletion_authorized(root)
    except ContractError:
        return False


def verify_manifest(root: Path, *, require_agent_log: bool = False) -> None:
    """Verify retained v1 artifacts; new callers should use the v2 verifier."""
    try:
        _verify_v1(root, require_agent_log=require_agent_log)
    except LegacyArtifactError as exc:
        raise ArtifactError(str(exc)) from exc


def _read_text(path: Path | None) -> str:
    if path is None or not path.is_file() or path.is_symlink():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _agent_output(clone: Path, explicit: Path | None) -> str:
    if explicit is not None:
        return _read_text(explicit)
    for path in (clone / "agent.log", clone / OUTPUT_DIR_NAME / "agent.log"):
        if path.is_file() and not path.is_symlink():
            return _read_text(path)
    return ""


def _result_payload(clone: Path) -> object | None:
    path = result_path(clone)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
        return parsed
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"parse_error": str(exc), "raw": _read_text(path)}


def _verification_records(clone: Path) -> list[dict[str, object]]:
    root = clone / OUTPUT_DIR_NAME / VERIFICATION_DIR_NAME
    if not root.is_dir() or root.is_symlink():
        return []
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": sha256_file(path),
                    "content": _read_text(path),
                }
            )
    return records


def _audit_payload(meta: WorkerMeta, audit: dict[str, object] | None) -> dict[str, Any]:
    values: dict[str, Any] = dict(audit or {})
    values.setdefault(
        "task_manifest",
        {"taskId": meta.task_id, "outcome": "legacy-compatible one-shot execution"},
    )
    values.setdefault("session", {"name": None, "closed": True, "mode": "one-shot"})
    values.setdefault(
        "cleanup_receipt",
        {
            "cloneDeletionAuthorized": meta.state == WorkerState.SUCCESS,
            "sessionClosed": True,
            "requestedState": str(meta.state),
        },
    )
    return values


def collect_artifacts(
    clone: Path,
    meta: WorkerMeta,
    artifact_root: Path,
    *,
    agent_log: Path | None = None,
    disposable_root: Path | None = None,
    audit: dict[str, object] | None = None,
) -> Path:
    """Atomically publish an exact three-file artifact directory outside the clone."""
    if not meta.base_commit:
        raise ArtifactError("base_commit required for artifact collection")
    final = artifact_root / meta.task_id
    staging = artifact_root / f"{STAGING_PREFIX}{meta.task_id}"
    if disposable_root is not None:
        for label, path in (("final", final), ("staging", staging)):
            if not artifact_outside_clone(path, clone, disposable_root):
                raise ArtifactError(f"unsafe {label} artifact path: {path}")
    for label, path in (("final", final), ("staging", staging)):
        if path.exists() or path.is_symlink():
            raise ArtifactError(f"refusing preexisting {label} artifact path: {path}")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        collect_git_patch(clone, meta.base_commit, staging / "changes.patch")
    except PatchError as exc:
        raise ArtifactError(str(exc)) from exc

    values = _audit_payload(meta, audit)
    worker_payload: dict[str, object] = {
        "schema_version": 2,
        "task_manifest": values["task_manifest"],
        "lifecycle": meta.to_dict(),
        "session": values["session"],
        "activity_lease": values.get("activity_lease", {"available": False}),
        "agent_output": _agent_output(clone, agent_log),
    }
    receipt_payload: dict[str, object] = {
        "schema_version": 2,
        "result": _result_payload(clone),
        "verification": _verification_records(clone),
        "cleanup_receipt": values["cleanup_receipt"],
        "metrics": values.get("metrics", []),
    }
    write_artifact_contract(
        staging, worker_payload=worker_payload, verification_payload=receipt_payload
    )
    verify_artifact_contract(staging)
    os.replace(staging, final)
    verify_artifact_contract(final)
    meta.artifact_path = str(final.resolve())
    return final.resolve()


def artifacts_complete_and_verified(
    artifact_path: str | None, *, clone: Path, disposable_root: Path
) -> bool:
    if not artifact_path:
        return False
    root = Path(artifact_path)
    if not root.is_dir() or root.is_symlink():
        return False
    if not artifact_outside_clone(root, clone, disposable_root):
        return False
    try:
        if (root / "MANIFEST.sha256").is_file():
            verify_manifest(root)
        else:
            verify_artifact_contract(root)
    except ArtifactError:
        return False
    return True
