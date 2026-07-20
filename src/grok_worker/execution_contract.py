"""Bounded execution contract: targets, risk tags, verification matrix, subtasks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

# Risk tags that expand the final verification matrix (never narrow a failed gate).
RISK_TAGS = frozenset(
    {
        "api",
        "schema",
        "security",
        "cache",
        "concurrency",
        "build",
        "migration",
        "package",
        "sdist",
        "wheel",
    }
)

# Technology-neutral final requirements associated with risk tags. Concrete
# commands belong in finalGates; this generic worker must not assume Python.
_RISK_FINAL_GATES: dict[str, tuple[str, ...]] = {
    "api": ("repository-appropriate API contract verification",),
    "schema": ("repository-appropriate schema compatibility verification",),
    "security": ("repository-appropriate security regression verification",),
    "cache": ("repository-appropriate cache behavior verification",),
    "concurrency": ("repository-appropriate concurrency verification",),
    "build": ("repository-appropriate clean build verification",),
    "migration": ("repository-appropriate migration verification",),
    "package": ("repository-appropriate package and install smoke verification",),
    "sdist": ("source distribution build and install verification when applicable",),
    "wheel": ("wheel build and install verification when applicable",),
}


class ExecutionContractError(ValueError):
    """Invalid execution-contract fields on a task manifest or run input."""


@dataclass(frozen=True)
class NamedSubtask:
    """Explicit independent read-only subtask for optional subagent fan-out."""

    name: str
    goal: str
    readonly: bool = True

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "goal": self.goal, "readonly": self.readonly}

    @classmethod
    def from_dict(cls, data: object) -> NamedSubtask:
        if not isinstance(data, dict):
            raise ExecutionContractError("subtasks entries must be objects")
        name = data.get("name")
        goal = data.get("goal")
        if not isinstance(name, str) or not name.strip():
            raise ExecutionContractError("subtask.name must be a nonempty string")
        if not isinstance(goal, str) or not goal.strip():
            raise ExecutionContractError("subtask.goal must be a nonempty string")
        readonly = data.get("readonly", True)
        if not isinstance(readonly, bool):
            raise ExecutionContractError("subtask.readonly must be a boolean")
        if not readonly:
            raise ExecutionContractError(
                "subtasks must be read-only; overlapping writes are forbidden"
            )
        return cls(name=name.strip(), goal=goal.strip(), readonly=True)


@dataclass(frozen=True)
class ExecutionContract:
    """Optional bounded run guidance carried on the dynamic task suffix."""

    target_files: tuple[str, ...] = ()
    target_modules: tuple[str, ...] = ()
    known_failure_evidence: tuple[str, ...] = ()
    focused_checks: tuple[str, ...] = ()
    final_gates: tuple[str, ...] = ()
    risk_tags: tuple[str, ...] = ()
    subtasks: tuple[NamedSubtask, ...] = ()
    # Gates that previously failed for this logical task (must remain required).
    required_failed_gates: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> ExecutionContract:
        return cls()

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> ExecutionContract:
        if not data:
            return cls.empty()
        return cls(
            target_files=_string_tuple(data.get("targetFiles"), "targetFiles"),
            target_modules=_string_tuple(data.get("targetModules"), "targetModules"),
            known_failure_evidence=_string_tuple(
                data.get("knownFailureEvidence"), "knownFailureEvidence"
            ),
            focused_checks=_string_tuple(data.get("focusedChecks"), "focusedChecks"),
            final_gates=_string_tuple(data.get("finalGates"), "finalGates"),
            risk_tags=_normalize_risk_tags(data.get("riskTags")),
            subtasks=_parse_subtasks(data.get("subtasks")),
            required_failed_gates=_string_tuple(
                data.get("requiredFailedGates"), "requiredFailedGates"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.target_files:
            payload["targetFiles"] = list(self.target_files)
        if self.target_modules:
            payload["targetModules"] = list(self.target_modules)
        if self.known_failure_evidence:
            payload["knownFailureEvidence"] = list(self.known_failure_evidence)
        if self.focused_checks:
            payload["focusedChecks"] = list(self.focused_checks)
        if self.final_gates:
            payload["finalGates"] = list(self.final_gates)
        if self.risk_tags:
            payload["riskTags"] = list(self.risk_tags)
        if self.subtasks:
            payload["subtasks"] = [item.to_dict() for item in self.subtasks]
        if self.required_failed_gates:
            payload["requiredFailedGates"] = list(self.required_failed_gates)
        return payload

    def to_worker_prompt_dict(self) -> dict[str, object]:
        """Return editing guidance while keeping final execution runner-owned."""
        payload = self.to_dict()
        payload.pop("finalGates", None)
        payload.pop("requiredFailedGates", None)
        final_count = len(self.runner_final_gates())
        if final_count:
            payload["runnerOwnsFinalGates"] = True
            payload["runnerFinalGateCount"] = final_count
            payload["finalGateInstruction"] = (
                "Do not execute runner-owned final gates; the lifecycle runner executes them once."
            )
        return payload

    def runner_final_gates(self) -> tuple[str, ...]:
        """Concrete commands the lifecycle runner owns and executes exactly once."""
        return tuple(dict.fromkeys((*self.final_gates, *self.required_failed_gates)))

    def validate_runner_gates(self) -> None:
        """Reject task labels that cannot be executed from the clone root."""
        for command in self.runner_final_gates():
            if len(command.split()) == 1 and "/" not in command and "\\" not in command:
                raise ExecutionContractError(
                    f"final gate {command!r} must be an executable command, not a bare task name; "
                    "include the repository wrapper and working directory"
                )

    def signature(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def expanded_final_matrix(self) -> tuple[str, ...]:
        """Focused checks while editing; one complete risk-appropriate final matrix.

        Never drops a previously failed required gate in favor of a narrower check.
        """
        ordered: list[str] = []
        seen: set[str] = set()

        def add(items: tuple[str, ...] | list[str]) -> None:
            for item in items:
                key = item.strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                ordered.append(key)

        add(self.final_gates)
        for tag in self.risk_tags:
            add(_RISK_FINAL_GATES.get(tag, ()))
        add(self.required_failed_gates)
        if not ordered and self.focused_checks:
            # Without explicit final gates/risk, require the focused set once at end.
            add(self.focused_checks)
        return tuple(ordered)

    def merge_failed_gate(self, gate: str) -> ExecutionContract:
        """Record a failed required gate so later runs cannot narrow it away."""
        gate = gate.strip()
        if not gate:
            return self
        if gate in self.required_failed_gates:
            return self
        return ExecutionContract(
            target_files=self.target_files,
            target_modules=self.target_modules,
            known_failure_evidence=self.known_failure_evidence,
            focused_checks=self.focused_checks,
            final_gates=self.final_gates,
            risk_tags=self.risk_tags,
            subtasks=self.subtasks,
            required_failed_gates=(*self.required_failed_gates, gate),
        )


def assert_gates_not_narrowed(
    previous_required: tuple[str, ...] | list[str],
    proposed_final: tuple[str, ...] | list[str],
) -> None:
    """Refuse replacing a previously failed required gate with a narrower set."""
    prev = {item.strip() for item in previous_required if item and item.strip()}
    proposed = {item.strip() for item in proposed_final if item and item.strip()}
    missing = sorted(prev - proposed)
    if missing:
        raise ExecutionContractError(
            "cannot replace previously failed required gates with a narrower matrix: "
            + ", ".join(missing)
        )


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ExecutionContractError(f"{field_name} must be a string list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ExecutionContractError(f"{field_name} entries must be nonempty strings")
        out.append(item.strip())
    return tuple(out)


def _normalize_risk_tags(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ExecutionContractError("riskTags must be a string list")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ExecutionContractError("riskTags entries must be nonempty strings")
        tag = item.strip().lower()
        if tag not in RISK_TAGS:
            raise ExecutionContractError(
                f"unknown riskTag {item!r}; expected one of {sorted(RISK_TAGS)}"
            )
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return tuple(out)


def _parse_subtasks(value: object) -> tuple[NamedSubtask, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ExecutionContractError("subtasks must be a list")
    if len(value) > 3:
        raise ExecutionContractError("at most 3 named subtasks (hard subagent policy)")
    return tuple(NamedSubtask.from_dict(item) for item in value)


@dataclass
class SubagentObservation:
    """Requested vs observed subagent behavior when the event stream allows it."""

    requested: int
    observed: int | None
    available: bool
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "observed": self.observed,
            "available": self.available,
            "notes": self.notes,
        }


def observe_subagents_from_log(log_text: str, *, requested: int) -> SubagentObservation:
    """Best-effort parse of native event stream; never invent counts."""
    if not log_text:
        return SubagentObservation(
            requested=requested,
            observed=None,
            available=False,
            notes="no agent log",
        )
    # Count only exact top-level machine events. Prompt/final text frequently
    # mentions "subagent" and must never be treated as execution evidence.
    event_names = {"subagent_start", "subagent-start", "spawn_subagent"}
    hits = 0
    for line in log_text.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_name = payload.get("event", payload.get("type"))
        if isinstance(event_name, str) and event_name in event_names:
            hits += 1
    if hits == 0:
        return SubagentObservation(
            requested=requested,
            observed=None,
            available=False,
            notes="subagent events unavailable in native stream",
        )
    return SubagentObservation(
        requested=requested,
        observed=hits,
        available=True,
        notes="counted exact top-level native subagent events",
    )


__all__ = [
    "ExecutionContract",
    "ExecutionContractError",
    "NamedSubtask",
    "RISK_TAGS",
    "SubagentObservation",
    "assert_gates_not_narrowed",
    "observe_subagents_from_log",
]
