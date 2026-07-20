"""Execution contract, continuation, native result, tool policy, progress (0.7.0)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grok_worker.continuation import (
    ContinuationError,
    assert_continuation_usable,
    build_continuation_contract,
    clear_continuation,
    read_continuation,
    write_continuation,
)
from grok_worker.execution_contract import (
    ExecutionContract,
    ExecutionContractError,
    assert_gates_not_narrowed,
    observe_subagents_from_log,
)
from grok_worker.native_result import (
    extract_structured_result_from_text,
    persist_native_structured_result,
    worker_result_json_schema,
)
from grok_worker.prompt_cache import TaskManifest, build_one_shot_prompt
from grok_worker.result_schema import ResultError
from grok_worker.run_config import RunConfig, build_native_cmd
from grok_worker.tool_policy import ToolPolicy, apply_native_tool_flags


def test_execution_contract_expands_risk_and_preserves_failed_gates() -> None:
    contract = ExecutionContract.from_mapping(
        {
            "focusedChecks": ["pytest tests/test_foo.py -q"],
            "finalGates": ["pytest -q"],
            "riskTags": ["package", "build"],
            "requiredFailedGates": ["sdist present"],
        }
    )
    matrix = contract.expanded_final_matrix()
    assert "pytest -q" in matrix
    assert "sdist present" in matrix
    assert "repository-appropriate clean build verification" in matrix
    assert "repository-appropriate package and install smoke verification" in matrix
    # Previously failed gate cannot be dropped by a narrower proposal.
    with pytest.raises(ExecutionContractError):
        assert_gates_not_narrowed(matrix, ["pytest -q"])


def test_subtasks_hard_cap_and_readonly() -> None:
    with pytest.raises(ExecutionContractError):
        ExecutionContract.from_mapping(
            {"subtasks": [{"name": f"t{i}", "goal": "g", "readonly": True} for i in range(4)]}
        )
    with pytest.raises(ExecutionContractError):
        ExecutionContract.from_mapping(
            {"subtasks": [{"name": "w", "goal": "edit", "readonly": False}]}
        )


def test_manifest_parses_execution_nested() -> None:
    data = {
        "taskId": "t1",
        "outcome": "do it",
        "verification": ["pytest -q"],
        "constraints": ["high"],
        "boundaries": {},
        "iterationPolicy": "once",
        "stopWhen": "done",
        "pauseIf": "blocked",
        "execution": {
            "targetFiles": ["src/a.py"],
            "riskTags": ["schema"],
            "subtasks": [{"name": "scan", "goal": "read only", "readonly": True}],
        },
    }
    manifest = TaskManifest.from_dict(data)
    assert manifest.execution.target_files == ("src/a.py",)
    assert "schema" in manifest.execution.risk_tags
    dumped = manifest.to_dict()
    assert "execution" in dumped
    assert dumped["execution"]["targetFiles"] == ["src/a.py"]


def test_stable_prefix_excludes_execution_and_native_capture() -> None:
    from grok_worker.prompt_cache import ONESHOT_TASK_DELIMITER

    base = build_one_shot_prompt(None, "implementation", "task body")
    with_extra = build_one_shot_prompt(
        None,
        "implementation",
        "task body",
        dynamic_suffix_extra="--- GROK_EXECUTION_CONTRACT_V1 ---\n{}\n",
    )
    assert (
        base.split(ONESHOT_TASK_DELIMITER, 1)[0] == with_extra.split(ONESHOT_TASK_DELIMITER, 1)[0]
    )
    assert "GROK_EXECUTION_CONTRACT_V1" in with_extra
    assert "GROK_EXECUTION_CONTRACT_V1" not in base.split(ONESHOT_TASK_DELIMITER, 1)[0]


def test_tool_policy_signature_and_native_flags() -> None:
    policy = ToolPolicy.from_fields(
        disable_web_search=True,
        disallowed_tools=["WebSearch", "Bash"],
        allow_subagents=False,
        max_turns=40,
    )
    other = ToolPolicy.from_fields(disable_web_search=True)
    assert policy.signature() != other.signature()
    cmd = apply_native_tool_flags(["grok"], policy)
    assert "--disable-web-search" in cmd
    assert "--disallowed-tools" in cmd
    assert "WebSearch,Bash" in cmd
    assert "--no-subagents" in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "40"


def test_build_native_cmd_includes_json_schema_and_continue(tmp_path: Path) -> None:
    prompt = tmp_path / "p.md"
    prompt.write_text("hi\n", encoding="utf-8")
    cfg = RunConfig(
        source=tmp_path,
        prompt="x",
        backend="native",
        mode="implementation",
        allow_subagents=True,
        disable_web_search=True,
        max_turns=12,
        native_json_schema_result=True,
    )
    cmd = build_native_cmd(cfg, tmp_path, prompt, continue_session=True)
    assert "--continue" in cmd
    assert "--json-schema" in cmd
    assert "--disable-web-search" in cmd
    assert "--max-turns" in cmd
    assert "--reasoning-effort" in cmd
    schema_idx = cmd.index("--json-schema") + 1
    schema = json.loads(cmd[schema_idx])
    assert schema["required"] == worker_result_json_schema()["required"]


def test_native_result_extract_and_persist(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    ver = clone / ".grok-output" / "verification"
    ver.mkdir(parents=True)
    (ver / "ok.log").write_text("ok\n", encoding="utf-8")
    payload = {
        "schema_version": 1,
        "task_completed": True,
        "status": "completed",
        "summary": "done",
        "findings": [],
        "verification": [
            {
                "command": "true",
                "exit_code": 0,
                "log_path": ".grok-output/verification/ok.log",
            }
        ],
    }
    text = json.dumps({"text": "noise", "result": payload})
    result = persist_native_structured_result(clone, text, mode="implementation")
    assert result.task_completed is True
    written = json.loads((clone / ".grok-output" / "result.json").read_text(encoding="utf-8"))
    assert written["summary"] == "done"

    with pytest.raises(ResultError):
        extract_structured_result_from_text("not json at all")


def test_continuation_ttl_and_contract_mismatch(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / ".grok-worker").mkdir()
    policy = ToolPolicy.from_fields(allow_subagents=True)
    cont = build_continuation_contract(
        task_id="t",
        source_realpath="/src",
        clone_realpath=str(clone.resolve()),
        base_sha="abc",
        model="grok-4.5",
        reasoning_effort="high",
        tool_policy=policy,
        execution_signature=ExecutionContract.empty().signature(),
        mode="implementation",
        ttl_hours=24,
    )
    write_continuation(clone, cont)
    loaded = read_continuation(clone)
    assert loaded.contract_hash == cont.contract_hash
    assert_continuation_usable(
        loaded,
        task_id="t",
        source_realpath="/src",
        clone_realpath=str(clone.resolve()),
        base_sha="abc",
        model="grok-4.5",
        reasoning_effort="high",
        tool_policy=policy,
        execution_signature=ExecutionContract.empty().signature(),
        mode="implementation",
    )
    with pytest.raises(ContinuationError):
        assert_continuation_usable(
            loaded,
            task_id="t",
            source_realpath="/src",
            clone_realpath=str(clone.resolve()),
            base_sha="abc",
            model="grok-4.5",
            reasoning_effort="high",
            tool_policy=ToolPolicy.from_fields(disable_web_search=True),
            execution_signature=ExecutionContract.empty().signature(),
            mode="implementation",
        )
    with pytest.raises(ContinuationError, match="execution contract"):
        assert_continuation_usable(
            loaded,
            task_id="t",
            source_realpath="/src",
            clone_realpath=str(clone.resolve()),
            base_sha="abc",
            model="grok-4.5",
            reasoning_effort="high",
            tool_policy=policy,
            execution_signature=ExecutionContract.from_mapping(
                {"finalGates": ["make test"]}
            ).signature(),
            mode="implementation",
        )
    assert clear_continuation(clone) is True


def test_subagent_observation_unavailable_without_markers() -> None:
    obs = observe_subagents_from_log("plain text only", requested=2)
    assert obs.available is False
    assert obs.observed is None
    hit = observe_subagents_from_log('{"event":"subagent_start"}', requested=1)
    assert hit.available is True


def test_productive_progress_emits_attention_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grok_worker import productive_progress as pp

    clone = tmp_path / "c"
    (clone / ".grok-worker").mkdir(parents=True)
    (clone / ".grok-output").mkdir()
    events: list[str] = []

    def fake_emit(**kwargs: object) -> None:
        events.append(str(kwargs.get("reason_code")))

    monkeypatch.setattr(pp, "emit_completion_event", fake_emit)
    first = pp.evaluate_productive_progress(
        clone,
        model_turns=1,
        stall_turns=1,
        stall_seconds=99999,
        task_id="t",
        run_id="r",
        dispatcher_id=None,
        shared_cache_root=tmp_path,
    )
    assert first["productive"] is True
    stalled = pp.evaluate_productive_progress(
        clone,
        model_turns=2,
        stall_turns=1,
        stall_seconds=99999,
        task_id="t",
        run_id="r",
        dispatcher_id=None,
        shared_cache_root=tmp_path,
    )
    assert stalled["productive"] is False
    assert stalled["stalled"] is True
    assert stalled["attention_emitted"] is True
    assert events.count("no_productive_progress") == 1
    again = pp.evaluate_productive_progress(
        clone,
        model_turns=3,
        stall_turns=1,
        stall_seconds=99999,
        task_id="t",
        run_id="r",
        dispatcher_id=None,
        shared_cache_root=tmp_path,
    )
    assert again["stalled"] is True
    assert again["attention_emitted"] is False
    assert events.count("no_productive_progress") == 1


def test_productive_progress_without_turn_metrics_uses_time_not_poll_count(
    tmp_path: Path,
) -> None:
    from grok_worker import productive_progress as pp

    clone = tmp_path / "quiet"
    (clone / ".grok-worker").mkdir(parents=True)
    (clone / ".grok-output").mkdir()
    pp.evaluate_productive_progress(
        clone,
        model_turns=None,
        stall_turns=1,
        stall_seconds=99999,
        task_id="quiet",
        run_id="quiet-run",
        dispatcher_id=None,
        shared_cache_root=tmp_path,
        emit_attention=False,
    )
    second = pp.evaluate_productive_progress(
        clone,
        model_turns=None,
        stall_turns=1,
        stall_seconds=99999,
        task_id="quiet",
        run_id="quiet-run",
        dispatcher_id=None,
        shared_cache_root=tmp_path,
        emit_attention=False,
    )
    assert second["stalled"] is False
    assert second["turns_without_progress"] == 0
