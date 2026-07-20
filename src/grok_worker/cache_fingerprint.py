"""Stable provider-cache fingerprint helpers (no shared-write logical cwd)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from grok_worker.continuation import PROMPT_VERSION, logical_workspace_id
from grok_worker.prompt_cache import ONESHOT_TASK_DELIMITER


@dataclass(frozen=True)
class PromptFingerprint:
    """Stable prefix hash + logical workspace id for cache A/B metrics."""

    stable_prefix_hash: str
    logical_workspace_id: str
    prompt_version: str
    # Physical clone cwd remains unique; we do not rewrite Grok session roots.
    physical_cwd_unique: bool = True
    # Documented limitation: Grok sessions key by physical path; no safe portable
    # logical cwd without shared writes across clones.
    logical_cwd_applied: bool = False
    limitation: str = (
        "Grok Build sessions are keyed by physical --cwd; stable logical cwd is "
        "not applied to avoid unsafe shared session writes. Stable prompt/context "
        "fingerprinting is used instead; do not claim provider cache hits without "
        "A/B evidence from token metrics."
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "stable_prefix_hash": self.stable_prefix_hash,
            "logical_workspace_id": self.logical_workspace_id,
            "prompt_version": self.prompt_version,
            "physical_cwd_unique": self.physical_cwd_unique,
            "logical_cwd_applied": self.logical_cwd_applied,
            "limitation": self.limitation,
        }


def stable_prefix_from_prompt(full_prompt: str) -> str:
    """Split one-shot prompt into stable prefix (before dynamic delimiter)."""
    if ONESHOT_TASK_DELIMITER in full_prompt:
        return full_prompt.split(ONESHOT_TASK_DELIMITER, 1)[0]
    return full_prompt


def hash_stable_prefix(stable_prefix: str) -> str:
    return hashlib.sha256(stable_prefix.encode()).hexdigest()


def build_prompt_fingerprint(full_prompt: str, *, source_realpath: str) -> PromptFingerprint:
    stable = stable_prefix_from_prompt(full_prompt)
    return PromptFingerprint(
        stable_prefix_hash=hash_stable_prefix(stable),
        logical_workspace_id=logical_workspace_id(source_realpath),
        prompt_version=PROMPT_VERSION,
    )


def cache_ab_metrics_record(
    *,
    fingerprint: PromptFingerprint,
    input_tokens: int | None,
    cached_tokens: int | None,
    model_calls: int | None,
    process_duration_seconds: float | None,
    cache_ratio: float | None,
    cache_ratio_basis: str | None,
) -> dict[str, object]:
    """Fields for A/B of fresh vs cached input without claiming unproven hits."""
    return {
        "prompt_fingerprint": fingerprint.to_dict(),
        "fresh_input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "model_calls": model_calls,
        "process_duration_seconds": process_duration_seconds,
        "cache_ratio": cache_ratio,
        "cache_ratio_basis": cache_ratio_basis,
        # Explicit: metrics only; operators must compare runs for cache effect.
        "provider_cache_claim": "unproven_without_ab",
    }


def write_fingerprint_sidecar(clone: Path, fingerprint: PromptFingerprint) -> Path:
    """Store fingerprint under meta for audit; not part of stable prompt bytes."""
    path = clone / ".grok-worker" / "prompt-fingerprint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(fingerprint.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "PromptFingerprint",
    "build_prompt_fingerprint",
    "cache_ab_metrics_record",
    "hash_stable_prefix",
    "stable_prefix_from_prompt",
    "write_fingerprint_sidecar",
]
