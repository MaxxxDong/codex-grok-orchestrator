"""Native Windows platform seams for locking, identity, and command launch."""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
from pathlib import Path

import pytest

from grok_worker.locks import FileLock
from grok_worker.process_identity import process_matches, process_start_token
from grok_worker.run_config import (
    RunConfig,
    build_acpx_cmd,
    resolve_acpx_command,
    resolve_executable,
)
from grok_worker.safety import safe_rmtree

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific contract")


def _try_lock(path: str, shared: bool, output: mp.Queue) -> None:
    lock = FileLock(Path(path), shared=shared)
    acquired = lock.try_acquire()
    output.put(acquired)
    if acquired:
        lock.release()


def _child_try_lock(path: Path, *, shared: bool) -> bool:
    ctx = mp.get_context("spawn")
    output: mp.Queue = ctx.Queue()
    child = ctx.Process(target=_try_lock, args=(str(path), shared, output))
    child.start()
    child.join(timeout=10)
    assert not child.is_alive()
    assert child.exitcode == 0
    return bool(output.get(timeout=2))


def test_windows_exclusive_lock_blocks_other_process(tmp_path: Path) -> None:
    path = tmp_path / "exclusive.lock"
    lock = FileLock(path)
    lock.acquire()
    try:
        assert _child_try_lock(path, shared=False) is False
    finally:
        lock.release()
    assert _child_try_lock(path, shared=False) is True


def test_windows_shared_locks_overlap_and_block_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "shared.lock"
    lock = FileLock(path, shared=True)
    lock.acquire()
    try:
        assert _child_try_lock(path, shared=True) is True
        assert _child_try_lock(path, shared=False) is False
    finally:
        lock.release()


def test_windows_process_identity_uses_creation_time() -> None:
    token = process_start_token(os.getpid())
    assert token is not None
    assert token.startswith("winfiletime:")
    assert process_matches(os.getpid(), token)
    assert not process_matches(os.getpid(), "winfiletime:0")


def test_windows_resolves_command_wrappers_from_path() -> None:
    resolved = Path(resolve_executable("acpx"))
    assert resolved.is_file()
    assert resolved.suffix.lower() in {".cmd", ".exe"}


def test_windows_acpx_bypasses_batch_for_multiline_prompts() -> None:
    command = resolve_acpx_command("acpx")
    assert Path(command[0]).name.lower() == "node.exe"
    assert Path(command[1]).as_posix().endswith("/node_modules/acpx/dist/cli.js")


def test_windows_agent_path_uses_forward_slashes_for_acpx(tmp_path: Path) -> None:
    agent = tmp_path / "agent.exe"
    agent.write_bytes(b"placeholder")
    cfg = RunConfig(source=tmp_path, prompt="test", model="test-model")
    command = build_acpx_cmd(cfg, tmp_path, str(agent), "prompt")
    value = command[command.index("--agent") + 1]
    assert "\\" not in value
    assert value.endswith("/agent.exe")


def test_windows_rmtree_retries_transient_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disposable = tmp_path / "workers"
    target = disposable / "worker"
    target.mkdir(parents=True)
    (target / "result.txt").write_text("done", encoding="utf-8")

    import grok_worker.safety as safety

    original_rmtree = safety.shutil.rmtree
    calls = 0

    def flaky_rmtree(path: Path, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            error = PermissionError(13, "directory is in use", str(path))
            error.winerror = 32
            raise error
        original_rmtree(path, **kwargs)

    monkeypatch.setattr(safety.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(safety.time, "sleep", lambda _seconds: None)

    safe_rmtree(target, disposable_root=disposable, protected=[])

    assert calls == 3
    assert not target.exists()
