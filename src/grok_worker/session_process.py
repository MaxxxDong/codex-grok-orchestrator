"""Process, prompt, token-metric, and configuration helpers for named sessions."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from grok_worker.cache_policy import (
    DEFAULT_CACHE_MAX_BYTES,
    DEFAULT_CACHE_TTL_HOURS,
    cache_use_lease,
)
from grok_worker.constants import DEFAULT_CAP_BYTES, MAX_CONCURRENT_WORKERS
from grok_worker.deps import prepare_shared_env, worker_env_exports
from grok_worker.locks import worker_lock
from grok_worker.metrics import append_metric, extract_token_metrics_from_text
from grok_worker.paths import meta_dir
from grok_worker.run_config import (
    default_agent_bin,
    normalize_agent_command,
    resolve_acpx_command,
)
from grok_worker.session_commands import build_ensure_cmd, build_prompt_cmd
from grok_worker.session_state import SessionState, permission_signature, session_state_path
from grok_worker.settings import (
    agent_policy_environment,
    default_model,
    default_reasoning_effort,
)


@dataclass
class SessionConfig:
    source: Path
    manifest_file: Path
    role: str
    mode: str
    disposable_root: Path
    artifact_root: Path
    shared_cache_root: Path
    acpx_bin: str = "acpx"
    agent_bin: str | None = None
    mcp_config: str | None = None
    model: str = ""
    reasoning_effort: str = ""
    allow_subagents: bool = False
    timeout: int = 1800
    prepare_deps: bool = True
    max_workers: int = MAX_CONCURRENT_WORKERS
    cap_bytes: int = DEFAULT_CAP_BYTES
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
class SessionOutcome:
    task_id: str
    state: str
    prompt_count: int
    clone_path: str | None
    artifact_path: str | None = None


def permission_contract_signature(cfg: SessionConfig) -> str:
    return permission_signature(
        mode=cfg.mode,
        agent=cfg.agent_bin or default_agent_bin(),
        mcp_config=cfg.mcp_config,
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort,
        allow_subagents=cfg.allow_subagents,
    )


def common_command(cfg: SessionConfig, clone: Path) -> list[str]:
    command = [
        *resolve_acpx_command(cfg.acpx_bin),
        "--cwd",
        str(clone),
        "--agent",
        normalize_agent_command(cfg.agent_bin or default_agent_bin()),
        "--auth-policy",
        "skip",
    ]
    if cfg.mcp_config:
        command.extend(["--mcp-config", cfg.mcp_config])
    command.extend(
        [
            "--model",
            cfg.model,
            "--timeout",
            str(cfg.timeout),
            "--format",
            "json",
            "--suppress-reads",
        ]
    )
    if cfg.mode == "analysis":
        command.extend(
            ["--approve-reads", "--non-interactive-permissions", "fail", "--no-terminal"]
        )
    else:
        command.append("--approve-all")
    return command


def invoke(command: list[str], log: Path, env: dict[str, str]) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command, capture_output=True, env=env, check=False)
    with log.open("ab") as stream:
        stream.write(result.stdout)
        stream.write(result.stderr)
    return int(result.returncode)


def prompt_turn(cfg: SessionConfig, state: SessionState, prompt: str, *, ensure: bool) -> int:
    clone = Path(state.clone_realpath)
    prompt_file = meta_dir(clone) / f"prompt-{state.prompt_count + 1:03d}.md"
    log = cfg.artifact_root / f".run-log-{state.task_id}" / "agent.log"
    env = os.environ.copy()
    env.update(
        agent_policy_environment(
            model=cfg.model,
            reasoning_effort=cfg.reasoning_effort,
            allow_subagents=cfg.allow_subagents,
        )
    )
    env["GROK_WORKER_LIFECYCLE"] = "1"
    env["GROK_WORKER_TASK_ID"] = state.task_id
    lease = cache_use_lease(cfg.shared_cache_root)
    with worker_lock(meta_dir(clone)), lease:
        dep_env: dict[str, str] = {}
        if cfg.prepare_deps:
            dep_env = prepare_shared_env(clone, cfg.shared_cache_root)
        effective_prompt = prompt
        if ensure and dep_env:
            effective_prompt += "\n" + worker_env_exports(dep_env)
        prompt_file.write_text(effective_prompt, encoding="utf-8")
        env.update(dep_env)
        common = common_command(cfg, clone)
        if ensure:
            ensure_exit = invoke(build_ensure_cmd(common, state.session_name), log, env)
            if ensure_exit != 0:
                state.status = "session_error"
                state.write(session_state_path(cfg.disposable_root, state.task_id))
                return ensure_exit
        exit_code = invoke(build_prompt_cmd(common, state.session_name, prompt_file), log, env)
    state.session_created = True
    state.prompt_count += 1
    state.status = "session_open" if exit_code == 0 else "session_error"
    state.write(session_state_path(cfg.disposable_root, state.task_id))
    try:
        log_text = log.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        log_text = ""
    append_metric(
        cfg.shared_cache_root / "metrics" / "worker-runs.jsonl",
        {"task_id": state.task_id, "prompt_count": state.prompt_count},
        extract_token_metrics_from_text(log_text),
    )
    return exit_code
