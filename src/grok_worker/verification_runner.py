"""Runner-owned execution and evidence capture for explicit final gates."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from shutil import which

from grok_worker.constants import OUTPUT_DIR_NAME, VERIFICATION_DIR_NAME
from grok_worker.models import VerificationRecord, WorkerResult, atomic_replace
from grok_worker.process_launch import hidden_startup_info

_POWERSHELL_PREFIX_RE = re.compile(
    r"^\s*(?:\$|\[|(?:Get|Set|New|Remove|Move|Copy|Write|Test|Resolve|ConvertTo|"
    r"ForEach|Where|Select|Invoke|Start|Stop)-|(?:pwsh|powershell)(?:\.exe)?\b)",
    re.IGNORECASE,
)
_QUOTED_EXECUTABLE_RE = re.compile(r'^\s*"[^"]+"\s+')


class VerificationRunnerError(RuntimeError):
    """Runner-owned verification could not be executed or recorded safely."""


def _verification_root(clone: Path) -> Path:
    root = clone / OUTPUT_DIR_NAME / VERIFICATION_DIR_NAME
    for candidate in (clone / OUTPUT_DIR_NAME, root):
        if candidate.is_symlink():
            raise VerificationRunnerError("verification output path must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.resolve().relative_to(clone.resolve())
    except (OSError, ValueError) as exc:
        raise VerificationRunnerError("verification output escaped the worker clone") from exc
    return root


def _command_argv(command: str) -> list[str]:
    if os.name != "nt":
        return ["/bin/sh", "-lc", command]
    if (
        "\n" in command
        or "\r" in command
        or _POWERSHELL_PREFIX_RE.search(command)
        or _QUOTED_EXECUTABLE_RE.search(command)
    ):
        pwsh = which("pwsh")
        if not pwsh:
            raise VerificationRunnerError("PowerShell final gate requires pwsh")
        prefix = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);"
        routed = f"& {command}" if _QUOTED_EXECUTABLE_RE.search(command) else command
        exit_guard = (
            "; if (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 } }"
            "; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"
        )
        return [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"{prefix} {routed}{exit_guard}",
        ]
    comspec = os.environ.get("COMSPEC") or which("cmd.exe") or "cmd.exe"
    return [comspec, "/d", "/s", "/c", command]


def _run_gate(
    clone: Path,
    command: str,
    *,
    index: int,
    env: dict[str, str],
    timeout_seconds: int,
) -> VerificationRecord:
    if not command.strip():
        raise VerificationRunnerError("final gate command must be nonempty")
    root = _verification_root(clone)
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
    name = f"runner-gate-{index:02d}-{digest}.log"
    final = root / name
    fd, tmp_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=root)
    exit_code = 1
    process: subprocess.Popen[bytes] | None = None
    try:
        with os.fdopen(fd, "wb") as stream:
            process = subprocess.Popen(
                _command_argv(command),
                cwd=clone,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                startupinfo=hidden_startup_info(),
                creationflags=(
                    int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if sys.platform == "win32"
                    else 0
                ),
                start_new_session=os.name != "nt",
            )
            try:
                exit_code = int(process.wait(timeout=timeout_seconds))
            except subprocess.TimeoutExpired:
                from grok_worker.activity_lease import terminate_process_tree

                terminate_process_tree(process)
                timeout_line = (
                    f"\n[grok-worker] verification timed out after {timeout_seconds}s\n"
                )
                stream.write(timeout_line.encode())
                exit_code = 124
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace(tmp_name, final)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    rel = final.relative_to(clone).as_posix()
    return VerificationRecord(command=command.strip(), exit_code=exit_code, log_path=rel)


def capture_final_gate_evidence(
    clone: Path,
    result: WorkerResult,
    final_gates: tuple[str, ...],
    *,
    env: dict[str, str],
    timeout_seconds: int,
) -> WorkerResult:
    """Execute explicit final gates and replace same-command model claims."""
    if not final_gates:
        return result
    captured = [
        _run_gate(
            clone,
            command,
            index=index,
            env=env,
            timeout_seconds=timeout_seconds,
        )
        for index, command in enumerate(final_gates, start=1)
    ]
    commands = {record.command for record in captured}
    result.verification = [
        record for record in result.verification if record.command not in commands
    ] + captured
    return result


__all__ = ["VerificationRunnerError", "capture_final_gate_evidence"]
