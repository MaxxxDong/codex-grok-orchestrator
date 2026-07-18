"""A named session keeps one clone, validates follow-ups, then finalizes once."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from grok_worker.dispatcher import count_held_slots
from tests.conftest import init_git_repo


def _write_manifest(path: Path, outcome: str, *, task_id: str = "session-flow") -> None:
    path.write_text(
        json.dumps(
            {
                "taskId": task_id,
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


def _session_cfg(
    tmp_path: Path,
    path_with_fake_acpx: Path,
    *,
    dispatcher_id: str | None = "disp-session",
    task_id: str = "session-flow",
) -> Any:
    from grok_worker.session_process import SessionConfig

    source = tmp_path / "source"
    if not (source / ".git").exists():
        init_git_repo(source)
    manifest = tmp_path / "task.json"
    _write_manifest(manifest, "implement first slice", task_id=task_id)
    return SessionConfig(
        source=source,
        manifest_file=manifest,
        role="implement",
        mode="implementation",
        disposable_root=tmp_path / "disposable",
        artifact_root=tmp_path / "artifacts",
        shared_cache_root=tmp_path / "cache",
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        dispatcher_id=dispatcher_id,
    )


def test_named_session_start_followup_finalize(tmp_path: Path, path_with_fake_acpx: Path) -> None:
    from grok_worker.session_runtime import (
        finalize_session,
        followup_session,
        start_session,
    )

    cfg = _session_cfg(tmp_path, path_with_fake_acpx, dispatcher_id=None)
    started = start_session(cfg)
    assert started.state == "session_open"
    clone = Path(started.clone_path or "")
    assert clone.is_dir()

    _write_manifest(cfg.manifest_file, "verify and repair the same slice")
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


class _HeldLease:
    """Minimal stand-in that records hold/release for ordering tests."""

    def __init__(self, events: list[str], tag: str = "lease") -> None:
        self.events = events
        self.tag = tag
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.events.append(f"{self.tag}:release")


def test_start_session_reserves_before_create_workspace(
    tmp_path: Path, path_with_fake_acpx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dispatcher_id: lease before clone; release after successful prompt."""
    from grok_worker import session_runtime as sr
    from grok_worker.session_runtime import start_session

    events: list[str] = []
    real_create = sr.create_workspace
    real_prompt = sr.prompt_turn
    held: list[_HeldLease] = []

    def fake_reserve(*_a: object, **_k: object) -> _HeldLease:
        events.append("lease:acquire")
        lease = _HeldLease(events)
        held.append(lease)
        return lease

    def fake_create(*a: object, **k: object) -> object:
        events.append("create_workspace")
        assert events[0] == "lease:acquire", "must reserve before clone"
        return real_create(*a, **k)

    def fake_prompt(*a: object, **k: object) -> None:
        events.append("prompt_turn")
        return real_prompt(*a, **k)

    monkeypatch.setattr(sr, "reserve_dispatcher_capacity", fake_reserve)
    monkeypatch.setattr(sr, "create_workspace", fake_create)
    monkeypatch.setattr(sr, "prompt_turn", fake_prompt)

    cfg = _session_cfg(tmp_path, path_with_fake_acpx, task_id="lease-start")
    started = start_session(cfg)
    assert started.state == "session_open"
    assert events[0] == "lease:acquire"
    assert "create_workspace" in events
    assert events.index("lease:acquire") < events.index("create_workspace")
    assert events.index("create_workspace") < events.index("prompt_turn")
    assert events[-1] == "lease:release"
    assert held[0].released is True


def test_start_session_releases_lease_on_create_error(
    tmp_path: Path, path_with_fake_acpx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grok_worker import session_runtime as sr
    from grok_worker.session_runtime import start_session

    events: list[str] = []
    held: list[_HeldLease] = []

    def fake_reserve(*_a: object, **_k: object) -> _HeldLease:
        events.append("lease:acquire")
        lease = _HeldLease(events)
        held.append(lease)
        return lease

    def boom(*_a: object, **_k: object) -> object:
        events.append("create_workspace")
        raise RuntimeError("clone failed")

    monkeypatch.setattr(sr, "reserve_dispatcher_capacity", fake_reserve)
    monkeypatch.setattr(sr, "create_workspace", boom)

    cfg = _session_cfg(tmp_path, path_with_fake_acpx, task_id="lease-create-err")
    with pytest.raises(RuntimeError, match="clone failed"):
        start_session(cfg)
    assert events == ["lease:acquire", "create_workspace", "lease:release"]
    assert held[0].released is True


def test_start_session_releases_lease_on_prompt_error(
    tmp_path: Path, path_with_fake_acpx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grok_worker import session_runtime as sr
    from grok_worker.session_runtime import start_session

    events: list[str] = []
    held: list[_HeldLease] = []

    def fake_reserve(*_a: object, **_k: object) -> _HeldLease:
        events.append("lease:acquire")
        lease = _HeldLease(events)
        held.append(lease)
        return lease

    def boom_prompt(*_a: object, **_k: object) -> None:
        events.append("prompt_turn")
        raise RuntimeError("prompt failed")

    monkeypatch.setattr(sr, "reserve_dispatcher_capacity", fake_reserve)
    monkeypatch.setattr(sr, "prompt_turn", boom_prompt)

    cfg = _session_cfg(tmp_path, path_with_fake_acpx, task_id="lease-prompt-err")
    with pytest.raises(RuntimeError, match="prompt failed"):
        start_session(cfg)
    assert "lease:acquire" in events
    assert "create_workspace" not in events or events.index("lease:acquire") < events.index(
        "prompt_turn"
    )
    assert events[-1] == "lease:release"
    assert held[0].released is True
    # Clone may exist; lease must still be free for reuse.
    assert count_held_slots(cfg.shared_cache_root, cfg.dispatcher_id or "") == 0


def test_followup_session_releases_lease_on_error(
    tmp_path: Path, path_with_fake_acpx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grok_worker import session_runtime as sr
    from grok_worker.session_runtime import followup_session, start_session

    cfg = _session_cfg(tmp_path, path_with_fake_acpx, task_id="lease-follow")
    start_session(cfg)
    assert count_held_slots(cfg.shared_cache_root, "disp-session") == 0

    events: list[str] = []
    held: list[_HeldLease] = []

    def fake_reserve(*_a: object, **_k: object) -> _HeldLease:
        events.append("lease:acquire")
        lease = _HeldLease(events)
        held.append(lease)
        return lease

    def boom_prompt(*_a: object, **_k: object) -> None:
        events.append("prompt_turn")
        raise RuntimeError("followup failed")

    monkeypatch.setattr(sr, "reserve_dispatcher_capacity", fake_reserve)
    monkeypatch.setattr(sr, "prompt_turn", boom_prompt)

    _write_manifest(cfg.manifest_file, "followup turn", task_id="lease-follow")
    with pytest.raises(RuntimeError, match="followup failed"):
        followup_session(cfg)
    assert events[0] == "lease:acquire"
    assert events[-1] == "lease:release"
    assert held[0].released is True


def test_finalize_holds_lease_through_finalize_run(
    tmp_path: Path, path_with_fake_acpx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lease covers close + state update + finalize_run; released only after."""
    from grok_worker import session_runtime as sr
    from grok_worker.session_runtime import finalize_session, start_session

    cfg = _session_cfg(tmp_path, path_with_fake_acpx, task_id="lease-final")
    start_session(cfg)

    events: list[str] = []
    held: list[_HeldLease] = []
    real_finalize = sr.finalize_run

    def fake_reserve(*_a: object, **_k: object) -> _HeldLease:
        events.append("lease:acquire")
        lease = _HeldLease(events)
        held.append(lease)
        return lease

    def wrap_finalize(*a: object, **k: object) -> object:
        assert held and not held[0].released, "lease must be held during finalize_run"
        events.append("finalize_run")
        return real_finalize(*a, **k)

    monkeypatch.setattr(sr, "reserve_dispatcher_capacity", fake_reserve)
    monkeypatch.setattr(sr, "finalize_run", wrap_finalize)

    final = finalize_session(cfg)
    assert final.state == "success"
    assert events[0] == "lease:acquire"
    assert "finalize_run" in events
    assert events.index("lease:acquire") < events.index("finalize_run")
    assert events[-1] == "lease:release"
    assert held[0].released is True


def test_finalize_releases_lease_when_close_raises(
    tmp_path: Path, path_with_fake_acpx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grok_worker import session_runtime as sr
    from grok_worker.session_runtime import finalize_session, start_session

    cfg = _session_cfg(tmp_path, path_with_fake_acpx, task_id="lease-close-err")
    start_session(cfg)

    events: list[str] = []
    held: list[_HeldLease] = []

    def fake_reserve(*_a: object, **_k: object) -> _HeldLease:
        events.append("lease:acquire")
        lease = _HeldLease(events)
        held.append(lease)
        return lease

    def boom_invoke(*_a: object, **_k: object) -> int:
        events.append("invoke")
        raise RuntimeError("close failed")

    monkeypatch.setattr(sr, "reserve_dispatcher_capacity", fake_reserve)
    monkeypatch.setattr(sr, "invoke", boom_invoke)

    with pytest.raises(RuntimeError, match="close failed"):
        finalize_session(cfg)
    assert events == ["lease:acquire", "invoke", "lease:release"]
    assert held[0].released is True
