"""Run configuration, outcome, and acpx command construction."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from grok_worker.acpx_runtime import resolve_managed_acpx_command
from grok_worker.cache_policy import DEFAULT_CACHE_MAX_BYTES, DEFAULT_CACHE_TTL_HOURS
from grok_worker.constants import (
    DEFAULT_ACPX_TIMEOUT,
    DEFAULT_CAP_BYTES,
    DEFAULT_FAILURE_RETAIN_HOURS,
    MAX_CONCURRENT_WORKERS,
)
from grok_worker.settings import default_model, default_reasoning_effort


@dataclass
class RunConfig:
    source: Path
    prompt: str
    disposable_root: Path | None = None
    artifact_root: Path | None = None
    shared_cache_root: Path | None = None
    cap_bytes: int = DEFAULT_CAP_BYTES
    keep_reason: str | None = None
    mode: str = "implementation"
    timeout: int = DEFAULT_ACPX_TIMEOUT
    task_id: str | None = None
    acpx_bin: str | None = None
    agent_bin: str | None = None
    mcp_config: str | None = None
    model: str = ""
    reasoning_effort: str = ""
    allow_subagents: bool = False
    failure_retain_hours: int = DEFAULT_FAILURE_RETAIN_HOURS
    prepare_deps: bool = True
    skip_pre_gc: bool = False
    skip_post_gc: bool = False
    include_dirty: bool = False
    max_workers: int = MAX_CONCURRENT_WORKERS
    cache_max_bytes: int = DEFAULT_CACHE_MAX_BYTES
    cache_ttl_hours: float = DEFAULT_CACHE_TTL_HOURS

    def __post_init__(self) -> None:
        if self.max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if not self.model:
            self.model = default_model()
        if not self.reasoning_effort:
            self.reasoning_effort = default_reasoning_effort()


@dataclass
class RunOutcome:
    task_id: str
    state: str
    exit_code: int
    clone_path: str | None
    artifact_path: str | None
    message: str


def default_agent_bin() -> str:
    here = Path(__file__).resolve()
    # src/grok_worker/run_config.py → parents[2] = package root with bin/
    installed = which("grok-worker-agent") or which("grok-acp-worker")
    if installed:
        return installed
    candidate = (
        here.parents[2]
        / "bin"
        / ("grok-acp-worker.cmd" if sys.platform == "win32" else "grok-acp-worker")
    )
    if candidate.is_file():
        return str(candidate)
    raise FileNotFoundError(
        "cannot locate grok-worker-agent; install the package or pass --agent-bin"
    )


def resolve_executable(command: str) -> str:
    """Resolve PATHEXT launchers on Windows while preserving explicit paths."""
    candidate = Path(command)
    if candidate.is_file():
        return str(candidate)
    return which(command) or command


def resolve_acpx_command(command: str | None) -> list[str]:
    """Resolve acpx without a Windows batch hop that truncates multiline prompts."""
    if command is None:
        if sys.platform == "win32":
            return resolve_managed_acpx_command()
        command = "acpx"
    resolved = Path(resolve_executable(command))
    if sys.platform == "win32" and resolved.suffix.lower() in {".cmd", ".bat"}:
        entry = resolved.parent / "node_modules" / "acpx" / "dist" / "cli.js"
        node = which("node")
        if entry.is_file() and node:
            return [node, str(entry)]
    return [str(resolved)]


def normalize_agent_command(command: str) -> str:
    """Protect native Windows paths from acpx's shell-style backslash parser."""
    candidate = Path(command)
    if sys.platform == "win32" and candidate.is_file():
        return candidate.as_posix()
    return command


def build_acpx_cmd(cfg: RunConfig, clone: Path, agent: str, prompt: str) -> list[str]:
    cmd = [
        *resolve_acpx_command(cfg.acpx_bin),
        "--cwd",
        str(clone),
        "--agent",
        normalize_agent_command(agent),
        "--auth-policy",
        "skip",
    ]
    if cfg.mcp_config:
        cmd.extend(["--mcp-config", cfg.mcp_config])
    cmd.extend(
        [
            "--model",
            cfg.model,
            "--timeout",
            str(cfg.timeout),
            "--format",
            "quiet",
            "--suppress-reads",
        ]
    )
    if cfg.mode == "analysis":
        cmd.extend(["--approve-reads", "--non-interactive-permissions", "fail", "--no-terminal"])
    else:
        cmd.append("--approve-all")
    cmd.extend(["exec", prompt])
    return cmd
