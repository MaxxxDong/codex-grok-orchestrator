"""Exact three-file external artifact contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from grok_worker.artifact_legacy import sha256_file

ARTIFACT_FILES = frozenset({"changes.patch", "worker.log", "verification.txt"})


class ContractError(RuntimeError):
    """The v2 artifact directory violates its external contract."""


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid JSON artifact {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"artifact must contain a JSON object: {path.name}")
    return value


def write_artifact_contract(
    root: Path,
    *,
    worker_payload: dict[str, object],
    verification_payload: dict[str, object],
) -> None:
    worker = root / "worker.log"
    worker.write_text(json.dumps(worker_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verification_payload["artifact_hashes"] = {
        "changes.patch": sha256_file(root / "changes.patch"),
        "worker.log": sha256_file(worker),
    }
    (root / "verification.txt").write_text(
        json.dumps(verification_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def verify_artifact_contract(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise ContractError(f"artifact is not a regular directory: {root}")
    entries = list(root.iterdir())
    invalid = [path.name for path in entries if path.is_symlink() or not path.is_file()]
    if invalid:
        raise ContractError(f"artifact contains non-regular entries: {sorted(invalid)}")
    actual = {path.name for path in entries}
    if actual != ARTIFACT_FILES:
        raise ContractError(
            f"artifact file set mismatch; expected {sorted(ARTIFACT_FILES)}, found {sorted(actual)}"
        )

    worker = _json_object(root / "worker.log")
    receipt = _json_object(root / "verification.txt")
    for key in ("task_manifest", "lifecycle", "session"):
        if not isinstance(worker.get(key), dict):
            raise ContractError(f"worker.log missing object: {key}")
    cleanup = receipt.get("cleanup_receipt")
    if not isinstance(cleanup, dict):
        raise ContractError("verification.txt missing cleanup_receipt")
    if not isinstance(cleanup.get("cloneDeletionAuthorized"), bool):
        raise ContractError("cleanup receipt lacks cloneDeletionAuthorized boolean")
    hashes = receipt.get("artifact_hashes")
    if not isinstance(hashes, dict) or set(hashes) != {"changes.patch", "worker.log"}:
        raise ContractError("verification.txt has invalid artifact_hashes")
    for name in ("changes.patch", "worker.log"):
        if hashes.get(name) != sha256_file(root / name):
            raise ContractError(f"artifact hash mismatch for {name}")


def clone_deletion_authorized(root: Path) -> bool:
    verify_artifact_contract(root)
    worker = _json_object(root / "worker.log")
    receipt = _json_object(root / "verification.txt")["cleanup_receipt"]
    session = worker["session"]
    return bool(
        isinstance(receipt, dict)
        and receipt.get("cloneDeletionAuthorized") is True
        and isinstance(session, dict)
        and session.get("closed") is True
    )
