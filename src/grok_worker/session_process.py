"""Process, prompt, token-metric, and configuration helpers for named sessions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from grok_worker.activity_lease import initialize_lease, run_with_activity_lease
from grok_worker.cache_policy import (
    DEFAULT_CACHE_MAX_BYTES,
    DEFAULT_CACHE_TTL_HOURS,
    cache_use_lease,
)
from grok_worker.constants import DEFAULT_ACPX_TIMEOUT, DEFAULT_CAP_BYTES, DEFAULT_HARD_TIMEOUT
from grok_worker.deps import prepare_shared_env
from grok_worker.grok_profile import scoped_worker_grok_home
from grok_worker.locks import worker_lock
from grok_worker.metrics import append_metric, extract_token_metrics_from_text
from grok_worker.paths import meta_dir
from grok_worker.run_config import default_agent_bin
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
    allow_subagents: bool = True
    timeout: int = DEFAULT_ACPX_TIMEOUT
    hard_timeout: int | None = DEFAULT_HARD_TIMEOUT
    prepare_deps: bool = True
    cap_bytes: int = DEFAULT_CAP_BYTES
    cache_max_bytes: int = DEFAULT_CACHE_MAX_BYTES
    cache_ttl_hours: float = DEFAULT_CACHE_TTL_HOURS
    dispatcher_id: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
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
        cfg.acpx_bin,
        "--cwd",
        str(clone),
        "--agent",
        cfg.agent_bin or default_agent_bin(),
        "--auth-policy",
        "skip",
    ]
    if cfg.mcp_config:
        command.extend(["--mcp-config", cfg.mcp_config])
    command.extend(
        [
            "--model",
            cfg.model,
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


def invoke(
    command: list[str],
    log: Path,
    env: dict[str, str],
    *,
    clone: Path,
    timeout: int,
    hard_timeout: int | None,
    initialize: bool = True,
) -> int:
    return run_with_activity_lease(
        command,
        clone=clone,
        log=log,
        env=env,
        idle_timeout_seconds=timeout,
        hard_timeout_seconds=hard_timeout,
        initialize=initialize,
    ).exit_code


def prompt_turn(cfg: SessionConfig, state: SessionState, prompt: str, *, ensure: bool) -> int:
    clone = Path(state.clone_realpath)
    prompt_file = meta_dir(clone) / f"prompt-{state.prompt_count + 1:03d}.md"
    prompt_file.write_text(prompt, encoding="utf-8")
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
    env["GROK_WORKER_GROK_HOME"] = str(scoped_worker_grok_home(clone, env))
    lease = cache_use_lease(cfg.shared_cache_root)
    with worker_lock(meta_dir(clone)), lease:
        dep_env: dict[str, str] = {}
        if cfg.prepare_deps:
            dep_env = prepare_shared_env(clone, cfg.shared_cache_root)
        env.update(dep_env)
        common = common_command(cfg, clone)
        initialize_lease(
            clone,
            idle_timeout_seconds=cfg.timeout,
            hard_timeout_seconds=cfg.hard_timeout,
        )
        if ensure:
            ensure_exit = invoke(
                build_ensure_cmd(common, state.session_name),
                log,
                env,
                clone=clone,
                timeout=cfg.timeout,
                hard_timeout=cfg.hard_timeout,
                initialize=False,
            )
            if ensure_exit != 0:
                state.status = "session_error"
                state.write(session_state_path(cfg.disposable_root, state.task_id))
                return ensure_exit
        exit_code = invoke(
            build_prompt_cmd(common, state.session_name, prompt_file),
            log,
            env,
            clone=clone,
            timeout=cfg.timeout,
            hard_timeout=cfg.hard_timeout,
            initialize=False,
        )
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
