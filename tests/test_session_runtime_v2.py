"""A named session keeps one clone, validates follow-ups, then finalizes once."""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import init_git_repo


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
