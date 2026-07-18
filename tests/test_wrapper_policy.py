"""Agent wrapper policy: configurable profile; hard-fail without lifecycle env."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]


def test_wrapper_policy_contents() -> None:
    wrapper = (CANDIDATE / "bin" / "grok-acp-worker").read_text(encoding="utf-8")
    entry = (CANDIDATE / "src" / "grok_worker" / "agent_entry.py").read_text(
        encoding="utf-8"
    )
    assert "GROK_WORKER_LIFECYCLE" in entry
    assert "GROK_WORKER_MODEL" in entry or "default_model" in entry
    assert "GROK_WORKER_ALLOW_SUBAGENTS" in entry
    assert "--no-subagents" in entry
    assert "--always-approve" in entry
    assert "prepare_isolated_profile" in entry
    assert "validate_isolated_profile" in entry
    assert "grok_worker.agent_entry" in wrapper


def test_wrapper_hard_fail_without_lifecycle() -> None:
    env = os.environ.copy()
    env.pop("GROK_WORKER_LIFECYCLE", None)
    env.pop("GROK_WORKER_ALLOW_DIRECT_AGENT", None)
    proc = subprocess.run(
        [str(CANDIDATE / "bin" / "grok-acp-worker")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 2
    assert "refusing" in proc.stderr.lower() or "GROK_WORKER_LIFECYCLE" in proc.stderr


def test_launcher_uses_platform_cache_contract() -> None:
    text = (CANDIDATE / "bin" / "grok-worker").read_text(encoding="utf-8")
    assert "GROK_WORKER_CACHE_ROOT" in text
    assert "Library/Caches/grok-worker" in text
    assert "uname -s" in text
