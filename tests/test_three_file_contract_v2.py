"""Successful external artifacts are exactly three files with embedded audit data."""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import init_git_repo


def test_three_file_artifact_contract_embeds_audit_and_hashes(tmp_path: Path) -> None:
    from grok_worker.artifacts import collect_artifacts, verify_artifact_contract
    from grok_worker.constants import MANAGED_BY, SCHEMA_VERSION
    from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now

    clone = tmp_path / "clone"
    base = init_git_repo(clone)
    (clone / "changed.txt").write_text("worker\n", encoding="utf-8")
    output = clone / ".grok-output"
    verification = output / "verification"
    verification.mkdir(parents=True)
    (verification / "tests.txt").write_text("1 passed\n", encoding="utf-8")
    (output / "result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_completed": True,
                "status": "completed",
                "summary": "done",
                "findings": [],
                "verification": [
                    {
                        "command": "pytest",
                        "exit_code": 0,
                        "log_path": ".grok-output/verification/tests.txt",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    agent_log = tmp_path / "agent.log"
    agent_log.write_text("worker output\n", encoding="utf-8")
    now = dt_to_iso(utc_now()) or ""
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="artifact-v2",
        source_realpath=str(clone),
        clone_realpath=str(clone),
        state=WorkerState.SUCCESS,
        created_at=now,
        updated_at=now,
        managed_by=MANAGED_BY,
        base_commit=base,
        exit_code=0,
        acpx_exit_code=0,
        artifact_complete=True,
    )
    artifacts = tmp_path / "artifacts"
    dest = collect_artifacts(
        clone,
        meta,
        artifacts,
        agent_log=agent_log,
        disposable_root=tmp_path / "disposable",
        audit={
            "task_manifest": {"taskId": "artifact-v2", "outcome": "test"},
            "session": {"name": "grok-artifact-v2", "closed": True},
            "cleanup_receipt": {"cloneDeletionAuthorized": True},
        },
    )
    assert sorted(p.name for p in dest.iterdir()) == [
        "changes.patch",
        "verification.txt",
        "worker.log",
    ]
    verify_artifact_contract(dest)
    log = json.loads((dest / "worker.log").read_text(encoding="utf-8"))
    receipt = json.loads((dest / "verification.txt").read_text(encoding="utf-8"))
    assert log["task_manifest"]["taskId"] == "artifact-v2"
    assert log["lifecycle"]["state"] == "success"
    assert log["session"]["closed"] is True
    assert receipt["cleanup_receipt"]["cloneDeletionAuthorized"] is True
    assert set(receipt["artifact_hashes"]) == {"changes.patch", "worker.log"}


def test_three_file_contract_rejects_extra_file(tmp_path: Path) -> None:
    from grok_worker.artifacts import ArtifactError, verify_artifact_contract

    root = tmp_path / "artifact"
    root.mkdir()
    for name in ("changes.patch", "worker.log", "verification.txt", "extra.json"):
        (root / name).write_text("{}", encoding="utf-8")
    try:
        verify_artifact_contract(root)
    except ArtifactError:
        pass
    else:  # pragma: no cover - RED until exact contract exists
        raise AssertionError("extra artifact files must be rejected")


def test_cleanup_receipt_is_a_separate_delete_gate(tmp_path: Path) -> None:
    from grok_worker.artifact_contract import write_artifact_contract
    from grok_worker.artifacts import artifact_authorizes_clone_deletion

    root = tmp_path / "artifact"
    root.mkdir()
    (root / "changes.patch").write_text("", encoding="utf-8")
    write_artifact_contract(
        root,
        worker_payload={
            "task_manifest": {},
            "lifecycle": {},
            "session": {"closed": True},
        },
        verification_payload={"cleanup_receipt": {"cloneDeletionAuthorized": False}},
    )
    assert not artifact_authorizes_clone_deletion(root)
