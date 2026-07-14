"""Named ACP sessions are reusable only inside one immutable logical-task contract."""

from __future__ import annotations

from pathlib import Path


def _state(tmp_path: Path):
    from grok_worker.session_state import SessionState

    return SessionState.new(
        task_id="same-task",
        source_realpath=str(tmp_path / "repo"),
        clone_realpath=str(tmp_path / "clone"),
        base_sha="abc",
        role="implement",
        mode="implementation",
        permission_signature="perm-a",
        session_name="grok-same-task",
        stable_prefix_hash="stable",
        context_pack_hash="pack",
    )


def test_followup_accepts_identical_contract(tmp_path: Path) -> None:
    from grok_worker.session_state import FollowupContract, validate_followup

    state = _state(tmp_path)
    contract = FollowupContract(
        task_id="same-task",
        source_realpath=state.source_realpath,
        base_sha="abc",
        role="implement",
        mode="implementation",
        permission_signature="perm-a",
    )
    validate_followup(state, contract)


def test_followup_rejects_new_task_role_or_permissions(tmp_path: Path) -> None:
    from grok_worker.session_state import FollowupContract, SessionContractError, validate_followup

    state = _state(tmp_path)
    base = dict(
        task_id="same-task",
        source_realpath=state.source_realpath,
        base_sha="abc",
        role="implement",
        mode="implementation",
        permission_signature="perm-a",
    )
    for field, value in (
        ("task_id", "new-task"),
        ("role", "review"),
        ("mode", "analysis"),
        ("permission_signature", "perm-b"),
        ("source_realpath", str(tmp_path / "other")),
    ):
        changed = dict(base)
        changed[field] = value
        try:
            validate_followup(state, FollowupContract(**changed))
        except SessionContractError:
            pass
        else:  # pragma: no cover - RED until contract guard exists
            raise AssertionError(f"followup should reject changed {field}")


def test_session_commands_use_same_name_and_close_explicitly(tmp_path: Path) -> None:
    from grok_worker.session_commands import build_close_cmd, build_ensure_cmd, build_prompt_cmd

    common = ["acpx", "--cwd", str(tmp_path), "--model", "grok-4.5"]
    ensure = build_ensure_cmd(common, "grok-task")
    prompt = build_prompt_cmd(common, "grok-task", tmp_path / "prompt.md")
    close = build_close_cmd(common, "grok-task")
    assert ensure[-4:] == ["sessions", "ensure", "--name", "grok-task"]
    assert prompt[-5:] == [
        "prompt",
        "--session",
        "grok-task",
        "--file",
        str(tmp_path / "prompt.md"),
    ]
    assert close[-3:] == ["sessions", "close", "grok-task"]
