"""Stable prompt construction, task manifests, and content-addressed context packs."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from grok_worker.execution_contract import (
    ExecutionContract,
    ExecutionContractError,
)


class ManifestError(ValueError):
    """Task manifest is missing required typed fields."""


class Role(StrEnum):
    IMPLEMENT = "implement"
    DEBUG = "debug"
    REVIEW = "review"
    RESEARCH = "research"


@dataclass(frozen=True)
class TaskManifest:
    task_id: str
    outcome: str
    verification: tuple[str, ...]
    constraints: tuple[str, ...]
    boundaries: dict[str, object]
    iteration_policy: str
    stop_when: str
    pause_if: str
    # Optional bounded execution contract (dynamic suffix only).
    execution: ExecutionContract = field(default_factory=ExecutionContract.empty)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TaskManifest:
        mapping = {
            "taskId": "task_id",
            "outcome": "outcome",
            "verification": "verification",
            "constraints": "constraints",
            "boundaries": "boundaries",
            "iterationPolicy": "iteration_policy",
            "stopWhen": "stop_when",
            "pauseIf": "pause_if",
        }
        missing = [key for key in mapping if key not in data]
        if missing:
            raise ManifestError(f"task manifest missing: {missing}")
        strings = ("taskId", "outcome", "iterationPolicy", "stopWhen", "pauseIf")
        if any(not isinstance(data[key], str) or not str(data[key]).strip() for key in strings):
            raise ManifestError("task manifest string fields must be nonempty")
        verification = data["verification"]
        constraints = data["constraints"]
        boundaries = data["boundaries"]
        if not isinstance(verification, list) or not all(isinstance(x, str) for x in verification):
            raise ManifestError("verification must be a string list")
        if not isinstance(constraints, list) or not all(isinstance(x, str) for x in constraints):
            raise ManifestError("constraints must be a string list")
        if not isinstance(boundaries, dict):
            raise ManifestError("boundaries must be an object")
        try:
            execution = ExecutionContract.from_mapping(_execution_mapping(data))
        except ExecutionContractError as exc:
            raise ManifestError(str(exc)) from exc
        return cls(
            task_id=str(data["taskId"]),
            outcome=str(data["outcome"]),
            verification=tuple(verification),
            constraints=tuple(constraints),
            boundaries={str(k): v for k, v in boundaries.items()},
            iteration_policy=str(data["iterationPolicy"]),
            stop_when=str(data["stopWhen"]),
            pause_if=str(data["pauseIf"]),
            execution=execution,
        )

    @classmethod
    def from_file(cls, path: Path) -> TaskManifest:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ManifestError("task manifest must be an object")
        return cls.from_dict(raw)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "taskId": self.task_id,
            "outcome": self.outcome,
            "verification": list(self.verification),
            "constraints": list(self.constraints),
            "boundaries": self.boundaries,
            "iterationPolicy": self.iteration_policy,
            "stopWhen": self.stop_when,
            "pauseIf": self.pause_if,
        }
        execution = self.execution.to_dict()
        if execution:
            payload["execution"] = execution
        return payload


def _execution_mapping(data: dict[str, object]) -> dict[str, Any] | None:
    """Accept nested execution object or flat optional fields on the manifest."""
    nested = data.get("execution")
    if isinstance(nested, dict):
        return {str(k): v for k, v in nested.items()}
    flat_keys = (
        "targetFiles",
        "targetModules",
        "knownFailureEvidence",
        "focusedChecks",
        "finalGates",
        "riskTags",
        "subtasks",
        "requiredFailedGates",
    )
    flat = {key: data[key] for key in flat_keys if key in data}
    return flat if flat else None


@dataclass(frozen=True)
class ContextPackRef:
    context_pack_hash: str
    path: Path


@dataclass(frozen=True)
class PromptBundle:
    stable_prefix: str
    stable_prefix_hash: str
    full_prompt: str
    followup_prompt: str


CANONICAL_PATHS = (
    "AGENTS.md",
    "README.md",
    "docs/architecture.md",
    "docs/current-state-and-gaps.md",
)
DELIMITER = "\n--- GROK_DYNAMIC_TASK_MANIFEST_V1 ---\n"
ONESHOT_TASK_DELIMITER = "\n--- GROK_ONE_SHOT_TASK_V1 ---\n"

# One-shot run modes → Skill role prompts (sessions choose roles explicitly).
ONESHOT_MODE_TO_ROLE: dict[str, Role] = {
    "implementation": Role.IMPLEMENT,
    "analysis": Role.REVIEW,
    # Prompt-only research one-shot (no source tree); never maps to implement.
    "research": Role.RESEARCH,
}


class OneShotModeError(ValueError):
    """One-shot mode is not supported for automatic role selection."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _repo_identity(source: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(source), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    stable = proc.stdout.strip() if proc.returncode == 0 else source.name
    return _sha256(stable.encode())[:20]


def build_context_pack(
    source: Path,
    base_sha: str,
    cache_root: Path,
    *,
    canonical_paths: tuple[str, ...] = CANONICAL_PATHS,
) -> ContextPackRef:
    files: list[dict[str, object]] = []
    for rel in canonical_paths:
        path = source / rel
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            files.append({"path": rel, "sha256": _sha256(content), "bytes": len(content)})
    payload = {
        "schemaVersion": "1.0",
        "repoIdentityHash": _repo_identity(source),
        "baseSha": base_sha,
        "files": files,
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    pack_hash = _sha256(encoded)
    directory = cache_root.resolve() / "context-packs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pack_hash}.json"
    if not path.exists():
        path.write_bytes(encoded)
    path.touch()
    return ContextPackRef(pack_hash, path)


def _load_base_and_role(_skill_root: Path | None, role: Role) -> str:
    prompt_root = Path(__file__).resolve().parent / "prompts"
    base = (prompt_root / "base-v1.md").read_text(encoding="utf-8")
    role_text = (prompt_root / f"role-{role.value}-v1.md").read_text(encoding="utf-8")
    return f"{base.rstrip()}\n\n{role_text.rstrip()}\n"


def role_for_one_shot_mode(mode: str) -> Role:
    """Map a one-shot RunConfig.mode to a Skill role; reject unknown modes."""
    try:
        return ONESHOT_MODE_TO_ROLE[mode]
    except KeyError as exc:
        supported = ", ".join(sorted(ONESHOT_MODE_TO_ROLE))
        raise OneShotModeError(
            f"unsupported one-shot mode {mode!r}; expected one of: {supported}"
        ) from exc


def build_one_shot_prompt(
    skill_root: Path | None,
    mode: str,
    task_prompt: str,
    *,
    dynamic_suffix_extra: str | None = None,
) -> str:
    """Compose Skill-owned base + role + caller task for a one-shot run.

    implementation → implement role (exact disk output contract in the role prompt).
    analysis → review role (read-only; lifecycle captures the response).

    Stable prefix (base + role) stays byte-stable for provider cache. Run-specific
    guidance (execution contract, native result capture) belongs only in the
    dynamic suffix after the one-shot delimiter.
    """
    role = role_for_one_shot_mode(mode)
    stable = _load_base_and_role(skill_root, role)
    task = task_prompt if task_prompt.endswith("\n") else f"{task_prompt.rstrip()}\n"
    extra = ""
    if dynamic_suffix_extra:
        extra = (
            dynamic_suffix_extra
            if dynamic_suffix_extra.endswith("\n")
            else f"{dynamic_suffix_extra.rstrip()}\n"
        )
    return f"{stable}{ONESHOT_TASK_DELIMITER}{task}{extra}"


def build_prompt(
    skill_root: Path | None,
    role: Role,
    context_pack: ContextPackRef,
    manifest: TaskManifest,
) -> PromptBundle:
    stable_roles = _load_base_and_role(skill_root, role)
    pack_text = context_pack.path.read_text(encoding="utf-8")
    stable = f"{stable_roles.rstrip()}\n\nCONTEXT_PACK\n{pack_text.rstrip()}\n"
    stable_hash = _sha256(stable.encode())
    dynamic = json.dumps(manifest.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
    return PromptBundle(
        stable_prefix=stable,
        stable_prefix_hash=stable_hash,
        full_prompt=stable + DELIMITER + dynamic + "\n",
        followup_prompt=DELIMITER + dynamic + "\n",
    )
