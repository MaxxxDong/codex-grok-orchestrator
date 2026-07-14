"""Environment-backed public defaults for worker and agent policy."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MODEL = "grok-4.5"
DEFAULT_REASONING_EFFORT = "high"


def env_text(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def default_model() -> str:
    return env_text("GROK_WORKER_MODEL", DEFAULT_MODEL)


def default_reasoning_effort() -> str:
    return env_text("GROK_WORKER_REASONING_EFFORT", DEFAULT_REASONING_EFFORT)


def default_mcp_config() -> str | None:
    explicit = os.environ.get("GROK_WORKER_MCP_CONFIG")
    if explicit is not None:
        value = explicit.strip()
        return str(Path(value).expanduser()) if value else None
    candidate = Path.home() / ".config" / "grok-worker" / "acpx-mcp.json"
    return str(candidate) if candidate.is_file() else None


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean flag")


def agent_policy_environment(
    *, model: str, reasoning_effort: str, allow_subagents: bool
) -> dict[str, str]:
    return {
        "GROK_WORKER_MODEL": model,
        "GROK_WORKER_REASONING_EFFORT": reasoning_effort,
        "GROK_WORKER_ALLOW_SUBAGENTS": "1" if allow_subagents else "0",
    }
