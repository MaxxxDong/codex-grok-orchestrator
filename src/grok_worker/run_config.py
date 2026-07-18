"""Run configuration, outcome, and backend command construction."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from grok_worker.cache_policy import DEFAULT_CACHE_MAX_BYTES, DEFAULT_CACHE_TTL_HOURS
from grok_worker.constants import (
    DEFAULT_ACPX_TIMEOUT,
    DEFAULT_CAP_BYTES,
    DEFAULT_FAILURE_RETAIN_HOURS,
    DEFAULT_HARD_TIMEOUT,
    MAX_CONCURRENT_WORKERS,
)
from grok_worker.settings import default_model, default_reasoning_effort


@dataclass
class RunConfig:
    source: Path | None
    prompt: str
    disposable_root: Path | None = None
    artifact_root: Path | None = None
    shared_cache_root: Path | None = None
    cap_bytes: int = DEFAULT_CAP_BYTES
    keep_reason: str | None = None
    mode: str = "implementation"
    timeout: int = DEFAULT_ACPX_TIMEOUT
    hard_timeout: int | None = DEFAULT_HARD_TIMEOUT
    task_id: str | None = None
    acpx_bin: str = "acpx"
    agent_bin: str | None = None
    mcp_config: str | None = None
    model: str = ""
    reasoning_effort: str = ""
    allow_subagents: bool = True
    failure_retain_hours: int = DEFAULT_FAILURE_RETAIN_HOURS
    prepare_deps: bool = True
    skip_pre_gc: bool = False
    skip_post_gc: bool = False
    include_dirty: bool = False
    include_dirty_paths: list[str] | None = None
    max_workers: int = MAX_CONCURRENT_WORKERS
    cache_max_bytes: int = DEFAULT_CACHE_MAX_BYTES
    cache_ttl_hours: float = DEFAULT_CACHE_TTL_HOURS
    dispatcher_id: str | None = None
    run_id: str | None = None
    prompt_only: bool = False
    # Library callers keep the v0.4 ACP default. The public `run` CLI passes
    # native explicitly, so existing embedded callers do not start real Grok
    # unexpectedly during an upgrade.
    backend: str = "acp"

    def __post_init__(self) -> None:
        if not self.model:
            self.model = default_model()
        if not self.reasoning_effort:
            self.reasoning_effort = default_reasoning_effort()
        if self.include_dirty_paths is None:
            self.include_dirty_paths = []
        if self.backend not in {"native", "acp"}:
            raise ValueError("backend must be native or acp")


@dataclass
class RunOutcome:
    task_id: str
    state: str
    exit_code: int
    clone_path: str | None
    artifact_path: str | None
    message: str
    run_id: str | None = None
    dispatcher_id: str | None = None


def default_agent_bin() -> str:
    here = Path(__file__).resolve()
    # src/grok_worker/run_config.py → parents[2] = package root with bin/
    candidate = here.parents[2] / "bin" / "grok-acp-worker"
    if candidate.is_file():
        return str(candidate)
    installed = which("grok-worker-agent") or which("grok-acp-worker")
    if installed:
        return installed
    raise FileNotFoundError(
        "cannot locate grok-worker-agent; install the package or pass --agent-bin"
    )


def build_acpx_cmd(cfg: RunConfig, clone: Path, agent: str, prompt: str) -> list[str]:
    cmd = [
        cfg.acpx_bin,
        "--cwd",
        str(clone),
        "--agent",
        agent,
        "--auth-policy",
        "skip",
    ]
    if cfg.mcp_config:
        cmd.extend(["--mcp-config", cfg.mcp_config])
    cmd.extend(
        [
            "--model",
            cfg.model,
            "--format",
            "quiet",
            "--suppress-reads",
        ]
    )
    if cfg.prompt_only:
        cmd.extend(["--approve-all", "--no-terminal"])
    elif cfg.mode in ("analysis", "research"):
        cmd.extend(["--approve-reads", "--non-interactive-permissions", "fail", "--no-terminal"])
    else:
        cmd.append("--approve-all")
    cmd.extend(["exec", prompt])
    return cmd


def default_grok_bin() -> str:
    configured = os.environ.get("GROK_WORKER_GROK_BIN")
    if configured:
        return configured
    configured = which("grok")
    if configured:
        return configured
    raise FileNotFoundError("cannot locate grok; install Grok Build or set PATH")


def build_native_cmd(cfg: RunConfig, clone: Path, prompt_file: Path) -> list[str]:
    read_only = cfg.mode in {"analysis", "research"}
    cmd = [
        default_grok_bin(),
        "--cwd",
        str(clone),
        "--sandbox",
        "read-only" if read_only else "workspace",
    ]
    if read_only:
        cmd.extend(["--permission-mode", "plan"])
    else:
        cmd.append("--always-approve")
    cmd.extend(
        [
            "--model",
            cfg.model,
            "--reasoning-effort",
            cfg.reasoning_effort,
        ]
    )
    if not cfg.allow_subagents:
        cmd.append("--no-subagents")
    cmd.extend(["--output-format", "json", "--prompt-file", str(prompt_file)])
    return cmd
