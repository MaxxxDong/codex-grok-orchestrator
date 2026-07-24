"""Persistent named-session state and immutable follow-up contract validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from grok_worker.models import atomic_write_text, dt_to_iso, utc_now


class SessionContractError(ValueError):
    """A follow-up attempted to cross a logical-task isolation boundary."""


@dataclass(frozen=True)
class FollowupContract:
    task_id: str
    source_realpath: str
    base_sha: str
    role: str
    mode: str
    permission_signature: str


@dataclass
class SessionState:
    schema_version: int
    task_id: str
    source_realpath: str
    clone_realpath: str
    base_sha: str
    role: str
    mode: str
    permission_signature: str
    session_name: str
    stable_prefix_hash: str
    context_pack_hash: str
    status: str
    created_at: str
    updated_at: str
    prompt_count: int = 0
    session_created: bool = False
    session_closed: bool = False

    @classmethod
    def new(
        cls,
        *,
        task_id: str,
        source_realpath: str,
        clone_realpath: str,
        base_sha: str,
        role: str,
        mode: str,
        permission_signature: str,
        session_name: str,
        stable_prefix_hash: str,
        context_pack_hash: str,
    ) -> SessionState:
        now = dt_to_iso(utc_now()) or ""
        return cls(
            schema_version=1,
            task_id=task_id,
            source_realpath=source_realpath,
            clone_realpath=clone_realpath,
            base_sha=base_sha,
            role=role,
            mode=mode,
            permission_signature=permission_signature,
            session_name=session_name,
            stable_prefix_hash=stable_prefix_hash,
            context_pack_hash=context_pack_hash,
            status="session_open",
            created_at=now,
            updated_at=now,
        )

    def write(self, path: Path) -> None:
        self.updated_at = dt_to_iso(utc_now()) or ""
        atomic_write_text(path, json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")

    @classmethod
    def read(cls, path: Path) -> SessionState:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionContractError(f"invalid session state: {exc}") from exc
        if not isinstance(raw, dict):
            raise SessionContractError("session state must be an object")
        expected = {field.name for field in __import__("dataclasses").fields(cls)}
        if set(raw) != expected:
            raise SessionContractError(
                f"session state fields mismatch: {sorted(set(raw) ^ expected)}"
            )
        try:
            return cls(
                schema_version=int(raw["schema_version"]),
                task_id=str(raw["task_id"]),
                source_realpath=str(raw["source_realpath"]),
                clone_realpath=str(raw["clone_realpath"]),
                base_sha=str(raw["base_sha"]),
                role=str(raw["role"]),
                mode=str(raw["mode"]),
                permission_signature=str(raw["permission_signature"]),
                session_name=str(raw["session_name"]),
                stable_prefix_hash=str(raw["stable_prefix_hash"]),
                context_pack_hash=str(raw["context_pack_hash"]),
                status=str(raw["status"]),
                created_at=str(raw["created_at"]),
                updated_at=str(raw["updated_at"]),
                prompt_count=int(raw["prompt_count"]),
                session_created=bool(raw["session_created"]),
                session_closed=bool(raw["session_closed"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionContractError(f"invalid session field value: {exc}") from exc


def permission_signature(
    *,
    mode: str,
    agent: str,
    mcp_config: str | None,
    model: str,
    reasoning_effort: str,
    allow_subagents: bool,
    acpx_runtime: str = "",
) -> str:
    payload = json.dumps(
        {
            "mode": mode,
            "agent": agent,
            "mcp_config": mcp_config,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "allow_subagents": allow_subagents,
            "acpx_runtime": acpx_runtime,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_followup(state: SessionState, contract: FollowupContract) -> None:
    expected = {
        "task_id": state.task_id,
        "source_realpath": state.source_realpath,
        "base_sha": state.base_sha,
        "role": state.role,
        "mode": state.mode,
        "permission_signature": state.permission_signature,
    }
    actual = asdict(contract)
    changed = sorted(key for key, value in expected.items() if actual[key] != value)
    if changed:
        raise SessionContractError(
            f"follow-up contract changed {changed}; create a new worker/session"
        )
    if state.session_closed or state.status not in {"session_open", "session_error"}:
        raise SessionContractError(f"session is not reusable: {state.status}")


def session_state_path(disposable_root: Path, task_id: str) -> Path:
    return disposable_root / ".session-state" / f"{task_id}.json"
