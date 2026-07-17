"""A named session keeps one clone, validates follow-ups, then finalizes once."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from tests.conftest import init_git_repo


def test_named_session_acpx_launch_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grok_worker import session_process

    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> object:
        captured["command"] = command
        captured.update(kwargs)
        return mock.Mock(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(session_process.subprocess, "run", fake_run)

    assert session_process.invoke(["acpx", "prompt"], tmp_path / "agent.log", {}) == 0
    startup_info = captured["startupinfo"]
    if os.name == "nt":
        assert isinstance(startup_info, subprocess.STARTUPINFO)
        assert startup_info.dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert startup_info.wShowWindow == subprocess.SW_HIDE
    else:
        assert startup_info is None


def _write_manifest(path: Path, outcome: str) -> None:
    path.write_text(
        json.dumps(
            {
                "taskId": "session-flow",
                "outcome": outcome,
                "verification": ["pytest -q"],
                "constraints": ["grok-4.5/high", "no Fast"],
                "boundaries": {"allowedWrites": ["."], "forbiddenWrites": []},
                "iterationPolicy": "continuous same-task iteration",
                "stopWhen": "tests pass",
                "pauseIf": "scope changes",
            }
        ),
        encoding="utf-8",
    )


def test_named_session_start_followup_finalize(tmp_path: Path, path_with_fake_acpx: Path) -> None:
    from grok_worker.session_runtime import (
        SessionConfig,
        finalize_session,
        followup_session,
        start_session,
    )

    source = tmp_path / "source"
    init_git_repo(source)
    manifest = tmp_path / "task.json"
    _write_manifest(manifest, "implement first slice")
    cfg = SessionConfig(
        source=source,
        manifest_file=manifest,
        role="implement",
        mode="implementation",
        disposable_root=tmp_path / "disposable",
        artifact_root=tmp_path / "artifacts",
        shared_cache_root=tmp_path / "cache",
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
    )
    started = start_session(cfg)
    assert started.state == "session_open"
    clone = Path(started.clone_path or "")
    assert clone.is_dir()

    _write_manifest(manifest, "verify and repair the same slice")
    followed = followup_session(cfg)
    assert followed.state == "session_open"
    assert followed.prompt_count == 2
    assert Path(followed.clone_path or "") == clone

    final = finalize_session(cfg)
    assert final.state == "success"
    assert final.clone_path is None
    artifact = Path(final.artifact_path or "")
    assert sorted(path.name for path in artifact.iterdir()) == [
        "changes.patch",
        "verification.txt",
        "worker.log",
    ]
    worker = json.loads((artifact / "worker.log").read_text(encoding="utf-8"))
    receipt = json.loads((artifact / "verification.txt").read_text(encoding="utf-8"))
    assert worker["session"]["closed"] is True
    assert worker["session"]["promptCount"] == 2
    assert len(receipt["metrics"]) == 2


def test_named_session_prepares_dependencies_once_under_prompt_lease(
    tmp_path: Path, path_with_fake_acpx: Path
) -> None:
    from grok_worker.session_process import SessionConfig
    from grok_worker.session_runtime import start_session

    source = tmp_path / "source"
    init_git_repo(source)
    manifest = tmp_path / "task.json"
    _write_manifest(manifest, "prepare one shared environment")
    calls = 0

    def prepare_once(_clone: Path, cache: Path) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {
            "UV_CACHE_DIR": str(cache / "uv"),
            "UV_PROJECT_ENVIRONMENT": str(cache / "venvs" / "test"),
        }

    cfg = SessionConfig(
        source=source,
        manifest_file=manifest,
        role="implement",
        mode="implementation",
        disposable_root=tmp_path / "disposable",
        artifact_root=tmp_path / "artifacts",
        shared_cache_root=tmp_path / "cache",
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=True,
    )
    with mock.patch(
        "grok_worker.session_runtime.prepare_shared_env", side_effect=prepare_once, create=True
    ):
        with mock.patch("grok_worker.session_process.prepare_shared_env", side_effect=prepare_once):
            started = start_session(cfg)

    assert started.state == "session_open"
    assert calls == 1


def test_named_session_config_rejects_non_positive_worker_limit(tmp_path: Path) -> None:
    from grok_worker.session_process import SessionConfig

    with pytest.raises(ValueError, match="max_workers must be at least 1"):
        SessionConfig(
            source=tmp_path,
            manifest_file=tmp_path / "task.json",
            role="implement",
            mode="implementation",
            disposable_root=tmp_path / "disposable",
            artifact_root=tmp_path / "artifacts",
            shared_cache_root=tmp_path / "cache",
            max_workers=0,
        )


def test_named_session_without_dependency_preparation_forbids_uv(
    tmp_path: Path, path_with_fake_acpx: Path
) -> None:
    from grok_worker.session_process import SessionConfig
    from grok_worker.session_runtime import start_session

    source = tmp_path / "source"
    init_git_repo(source)
    manifest = tmp_path / "task.json"
    _write_manifest(manifest, "use existing tools only")
    cfg = SessionConfig(
        source=source,
        manifest_file=manifest,
        role="implement",
        mode="implementation",
        disposable_root=tmp_path / "disposable",
        artifact_root=tmp_path / "artifacts",
        shared_cache_root=tmp_path / "cache",
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
    )

    started = start_session(cfg)
    prompt = (Path(started.clone_path or "") / ".grok-worker" / "prompt-001.md").read_text(
        encoding="utf-8"
    )

    assert "Dependency preparation is disabled" in prompt
    assert "Do not run uv, uv run, uv sync, pip" in prompt
    assert "Always use: uv run --no-sync" not in prompt


def test_session_state_read_rejects_unknown_fields(tmp_path: Path) -> None:
    from grok_worker.session_state import SessionContractError, SessionState

    path = tmp_path / "state.json"
    path.write_text('{"unexpected": true}', encoding="utf-8")
    try:
        SessionState.read(path)
    except SessionContractError:
        pass
    else:  # pragma: no cover
        raise AssertionError("invalid state must fail closed")
