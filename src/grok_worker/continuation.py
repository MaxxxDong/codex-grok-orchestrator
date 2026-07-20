"""Native same-task continuation metadata (explicit, TTL-bounded, non-ACP)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from grok_worker.constants import META_DIR_NAME
from grok_worker.models import atomic_write_text, dt_from_iso, dt_to_iso, utc_now
from grok_worker.tool_policy import ToolPolicy

CONTINUATION_SCHEMA_VERSION = 1
CONTINUATION_FILE_NAME = "continuation.json"
DEFAULT_CONTINUATION_TTL_HOURS = 24.0
PROMPT_VERSION = "stable-base-v1"


class ContinuationError(ValueError):
    """Continuation contract mismatch, expiry, or unsupported backend."""


@dataclass(frozen=True)
class ContinuationContract:
    """Minimal compatibility surface for resuming the same logical native task."""

    schema_version: int
    task_id: str
    source_realpath: str
    clone_realpath: str
    base_sha: str
    model: str
    reasoning_effort: str
    tool_signature: str
    execution_signature: str
    prompt_version: str
    contract_hash: str
    mode: str
    created_at: str
    expires_at: str
    run_id: str | None = None
    native_session_hint: str | None = None
    logical_workspace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContinuationContract:
        if int(data.get("schema_version", -1)) != CONTINUATION_SCHEMA_VERSION:
            raise ContinuationError("unsupported continuation schema_version")
        required = (
            "task_id",
            "source_realpath",
            "clone_realpath",
            "base_sha",
            "model",
            "reasoning_effort",
            "tool_signature",
            "execution_signature",
            "prompt_version",
            "contract_hash",
            "mode",
            "created_at",
            "expires_at",
        )
        missing = [key for key in required if key not in data]
        if missing:
            raise ContinuationError(f"continuation missing fields: {missing}")
        return cls(
            schema_version=CONTINUATION_SCHEMA_VERSION,
            task_id=str(data["task_id"]),
            source_realpath=str(data["source_realpath"]),
            clone_realpath=str(data["clone_realpath"]),
            base_sha=str(data["base_sha"]),
            model=str(data["model"]),
            reasoning_effort=str(data["reasoning_effort"]),
            tool_signature=str(data["tool_signature"]),
            execution_signature=str(data["execution_signature"]),
            prompt_version=str(data["prompt_version"]),
            contract_hash=str(data["contract_hash"]),
            mode=str(data["mode"]),
            created_at=str(data["created_at"]),
            expires_at=str(data["expires_at"]),
            run_id=_optional_str(data.get("run_id")),
            native_session_hint=_optional_str(data.get("native_session_hint")),
            logical_workspace_id=_optional_str(data.get("logical_workspace_id")),
        )


def continuation_path(clone: Path) -> Path:
    return clone / META_DIR_NAME / CONTINUATION_FILE_NAME


def compute_contract_hash(
    *,
    task_id: str,
    source_realpath: str,
    clone_realpath: str,
    base_sha: str,
    model: str,
    reasoning_effort: str,
    tool_signature: str,
    execution_signature: str,
    prompt_version: str,
    mode: str,
) -> str:
    payload = {
        "task_id": task_id,
        "source_realpath": source_realpath,
        "clone_realpath": clone_realpath,
        "base_sha": base_sha,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "tool_signature": tool_signature,
        "execution_signature": execution_signature,
        "prompt_version": prompt_version,
        "mode": mode,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def logical_workspace_id(source_realpath: str) -> str:
    """Stable logical identity for metrics/docs — not a shared writable cwd."""
    return hashlib.sha256(source_realpath.encode()).hexdigest()[:24]


def build_continuation_contract(
    *,
    task_id: str,
    source_realpath: str,
    clone_realpath: str,
    base_sha: str,
    model: str,
    reasoning_effort: str,
    tool_policy: ToolPolicy,
    execution_signature: str,
    mode: str,
    run_id: str | None = None,
    ttl_hours: float = DEFAULT_CONTINUATION_TTL_HOURS,
    native_session_hint: str | None = None,
) -> ContinuationContract:
    if ttl_hours <= 0:
        raise ContinuationError("continuation TTL must be positive")
    tool_sig = tool_policy.signature()
    prompt_version = PROMPT_VERSION
    contract_hash = compute_contract_hash(
        task_id=task_id,
        source_realpath=source_realpath,
        clone_realpath=clone_realpath,
        base_sha=base_sha or "",
        model=model,
        reasoning_effort=reasoning_effort,
        tool_signature=tool_sig,
        execution_signature=execution_signature,
        prompt_version=prompt_version,
        mode=mode,
    )
    now = utc_now()
    return ContinuationContract(
        schema_version=CONTINUATION_SCHEMA_VERSION,
        task_id=task_id,
        source_realpath=source_realpath,
        clone_realpath=clone_realpath,
        base_sha=base_sha or "",
        model=model,
        reasoning_effort=reasoning_effort,
        tool_signature=tool_sig,
        execution_signature=execution_signature,
        prompt_version=prompt_version,
        contract_hash=contract_hash,
        mode=mode,
        created_at=dt_to_iso(now) or "",
        expires_at=dt_to_iso(now + timedelta(hours=ttl_hours)) or "",
        run_id=run_id,
        native_session_hint=native_session_hint,
        logical_workspace_id=logical_workspace_id(source_realpath),
    )


def write_continuation(clone: Path, contract: ContinuationContract) -> Path:
    path = continuation_path(clone)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(contract.to_dict(), indent=2, sort_keys=True) + "\n")
    return path


def read_continuation(clone: Path) -> ContinuationContract:
    path = continuation_path(clone)
    if not path.is_file() or path.is_symlink():
        raise ContinuationError(f"missing continuation metadata: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContinuationError(f"invalid continuation metadata: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContinuationError("continuation metadata must be an object")
    return ContinuationContract.from_dict(raw)


def clear_continuation(clone: Path) -> bool:
    path = continuation_path(clone)
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    raise ContinuationError(f"refusing to clear non-file continuation path: {path}")


def assert_continuation_usable(
    contract: ContinuationContract,
    *,
    task_id: str,
    source_realpath: str,
    clone_realpath: str,
    base_sha: str | None,
    model: str,
    reasoning_effort: str,
    tool_policy: ToolPolicy,
    execution_signature: str,
    mode: str,
) -> None:
    """Enforce same-task, same-clone, same-model/tool signature continuation."""
    now = utc_now()
    expires = dt_from_iso(contract.expires_at)
    if expires is None or now > expires:
        raise ContinuationError("continuation expired; start a new one-shot run")
    expected_hash = compute_contract_hash(
        task_id=task_id,
        source_realpath=source_realpath,
        clone_realpath=clone_realpath,
        base_sha=base_sha or contract.base_sha,
        model=model,
        reasoning_effort=reasoning_effort,
        tool_signature=tool_policy.signature(),
        execution_signature=execution_signature,
        prompt_version=PROMPT_VERSION,
        mode=mode,
    )
    if contract.task_id != task_id:
        raise ContinuationError("continuation task_id mismatch")
    if contract.source_realpath != source_realpath:
        raise ContinuationError("continuation source_realpath mismatch")
    if contract.clone_realpath != clone_realpath:
        raise ContinuationError("continuation clone_realpath mismatch")
    if base_sha is not None and contract.base_sha and contract.base_sha != base_sha:
        raise ContinuationError("continuation base_sha mismatch")
    if contract.model != model or contract.reasoning_effort != reasoning_effort:
        raise ContinuationError("continuation model/reasoning mismatch")
    if contract.tool_signature != tool_policy.signature():
        raise ContinuationError("continuation tool signature mismatch")
    if contract.execution_signature != execution_signature:
        raise ContinuationError("continuation execution contract mismatch")
    if contract.mode != mode:
        raise ContinuationError("continuation mode mismatch")
    if contract.contract_hash != expected_hash:
        raise ContinuationError("continuation contract hash mismatch; start a new run")


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    return None


__all__ = [
    "CONTINUATION_FILE_NAME",
    "DEFAULT_CONTINUATION_TTL_HOURS",
    "PROMPT_VERSION",
    "ContinuationContract",
    "ContinuationError",
    "assert_continuation_usable",
    "build_continuation_contract",
    "clear_continuation",
    "compute_contract_hash",
    "continuation_path",
    "logical_workspace_id",
    "read_continuation",
    "write_continuation",
]
