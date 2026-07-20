"""Runner-owned final-gate evidence replaces unverifiable model claims."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from grok_worker.models import ResultStatus, VerificationRecord, WorkerResult
from grok_worker.result_schema import validate_verification_files
from grok_worker.verification_runner import capture_final_gate_evidence


def _command(exit_code: int) -> str:
    executable = f'"{sys.executable}"' if os.name == "nt" else sys.executable
    return f'{executable} -c "print(\'runner evidence\'); raise SystemExit({exit_code})"'


def _result(command: str) -> WorkerResult:
    return WorkerResult(
        schema_version=1,
        task_completed=True,
        status=ResultStatus.COMPLETED,
        summary="done",
        verification=[
            VerificationRecord(
                command=command,
                exit_code=0,
                log_path=".grok-output/verification/model-forgot-this.log",
            )
        ],
    )


def test_runner_replaces_same_command_missing_log_with_real_evidence(tmp_path: Path) -> None:
    command = _command(0)
    result = capture_final_gate_evidence(
        tmp_path,
        _result(command),
        (command,),
        env=dict(os.environ),
        timeout_seconds=10,
    )

    assert len(result.verification) == 1
    record = result.verification[0]
    assert record.exit_code == 0
    assert record.log_path.startswith(".grok-output/verification/runner-gate-")
    validate_verification_files(tmp_path, result)
    assert "runner evidence" in (tmp_path / record.log_path).read_text(
        encoding="utf-8"
    )


def test_runner_records_real_nonzero_gate_instead_of_model_success(tmp_path: Path) -> None:
    command = _command(7)
    result = capture_final_gate_evidence(
        tmp_path,
        _result(command),
        (command,),
        env=dict(os.environ),
        timeout_seconds=10,
    )

    assert result.verification[0].exit_code == 7
    validate_verification_files(tmp_path, result)


def test_runner_routes_explicit_pwsh_command_without_losing_nested_script(
    tmp_path: Path,
) -> None:
    if os.name != "nt" or shutil.which("pwsh") is None:
        return
    (tmp_path / "value.txt").write_text("after\n", encoding="utf-8")
    command = (
        "pwsh -NoProfile -Command \"if ((Get-Content -Raw value.txt).Trim() "
        "-ne 'after') { throw 'unexpected value' } else { 'verified' }\""
    )

    result = capture_final_gate_evidence(
        tmp_path,
        _result(command),
        (command,),
        env=dict(os.environ),
        timeout_seconds=10,
    )

    record = result.verification[0]
    assert record.exit_code == 0
    assert (tmp_path / record.log_path).read_text(encoding="utf-8").strip() == "verified"
