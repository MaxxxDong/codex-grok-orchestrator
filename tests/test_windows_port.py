"""Native Windows platform seams for locking, identity, and command launch."""

from __future__ import annotations

import multiprocessing as mp
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from grok_worker.locks import FileLock
from grok_worker.models import WorkerMeta, WorkerState, atomic_write_text, dt_to_iso, utc_now
from grok_worker.process_identity import process_matches, process_start_token
from grok_worker.run_config import (
    RunConfig,
    build_acpx_cmd,
    resolve_acpx_command,
    resolve_executable,
)
from grok_worker.safety import safe_rmtree
from grok_worker.worker_exec import execute_worker
from tests.conftest import init_git_repo
from tests.fake_acpx import write_fake_acpx

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
    cfg = RunConfig(source=tmp_path, prompt="test", model="test-model", acpx_bin="acpx")
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


def test_windows_atomic_write_waits_for_transient_reader(tmp_path: Path) -> None:
    target = tmp_path / "lifecycle.json"
    target.write_text("old", encoding="utf-8")
    ready = threading.Event()
    release = threading.Event()

    def hold_reader() -> None:
        with target.open("r", encoding="utf-8"):
            ready.set()
            release.wait(timeout=5)

    reader = threading.Thread(target=hold_reader)
    reader.start()
    assert ready.wait(timeout=2)
    timer = threading.Timer(0.2, release.set)
    timer.start()
    try:
        atomic_write_text(target, "new")
    finally:
        release.set()
        timer.cancel()
        reader.join(timeout=5)

    assert not reader.is_alive()
    assert target.read_text(encoding="utf-8") == "new"


def test_windows_atomic_write_stops_after_bounded_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grok_worker.models as models

    target = tmp_path / "lifecycle.json"
    target.write_text("old", encoding="utf-8")
    attempts = 0

    def always_busy(_source: str | Path, _target: Path) -> None:
        nonlocal attempts
        attempts += 1
        error = PermissionError(13, "target is busy", str(_target))
        error.winerror = 5
        raise error

    monkeypatch.setattr(models.os, "replace", always_busy)
    monkeypatch.setattr(models.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError):
        atomic_write_text(target, "new")

    assert attempts == 30
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".lifecycle-*.tmp"))


def test_windows_metadata_failure_reaps_started_acpx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    clone = tmp_path / "workers" / "grok-worker-orphan-test"
    artifacts = tmp_path / "artifacts"
    shared = tmp_path / "cache"
    for path in (source, clone, artifacts, shared):
        path.mkdir(parents=True)

    now = dt_to_iso(utc_now()) or ""
    meta = WorkerMeta(
        schema_version=1,
        task_id="orphan-test",
        source_realpath=str(source.resolve()),
        clone_realpath=str(clone.resolve()),
        state=WorkerState.CREATING,
        created_at=now,
        updated_at=now,
    )
    original_write = meta.write
    writes = 0

    def fail_acpx_pid_write(path: Path) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            error = PermissionError(13, "lifecycle target is busy", str(path))
            error.winerror = 5
            raise error
        original_write(path)

    meta.write = fail_acpx_pid_write  # type: ignore[method-assign]

    class FakeChild:
        pid = 999_999_991

        def __init__(self) -> None:
            self.terminated = False
            self.waited = False

        def poll(self) -> int | None:
            return 143 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
            self.waited = True
            return 143 if self.terminated else 0

    child = FakeChild()
    monkeypatch.setattr("grok_worker.activity_lease.subprocess.Popen", lambda *a, **k: child)
    monkeypatch.setattr("grok_worker.worker_exec.subprocess.run", lambda *a, **k: object())
    monkeypatch.setattr("grok_worker.worker_exec.process_start_token", lambda _pid: "token")

    cfg = RunConfig(
        source=source,
        prompt="bounded task",
        disposable_root=clone.parent,
        artifact_root=artifacts,
        shared_cache_root=shared,
        prepare_deps=False,
        task_id="orphan-test",
        skip_post_gc=True,
        acpx_bin="acpx",
    )

    with pytest.raises(PermissionError):
        execute_worker(
            cfg,
            clone,
            meta,
            clone.parent,
            artifacts,
            shared,
            [source],
            "fake-agent",
        )

    assert child.terminated
    assert child.waited


def test_windows_reaps_descendants_after_acpx_parent_exits() -> None:
    base_python = getattr(sys, "_base_executable", sys.executable)
    parent_code = (
        "import subprocess,sys; "
        "child=subprocess.Popen([sys._base_executable,'-c','import time; time.sleep(60)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "print(child.pid,flush=True)"
    )
    parent = subprocess.Popen(
        [base_python, "-c", parent_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline().strip())
    parent.wait(timeout=5)
    assert process_start_token(child_pid) is not None

    try:
        from grok_worker.worker_exec import _reap_process_tree

        _reap_process_tree(parent)
        deadline = time.monotonic() + 5
        while process_start_token(child_pid) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process_start_token(child_pid) is None
    finally:
        if process_start_token(child_pid) is not None:
            subprocess.run(
                ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                check=False,
                capture_output=True,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )


def test_windows_six_workers_run_in_parallel(tmp_path: Path) -> None:
    source = tmp_path / "source"
    init_git_repo(source)
    (source / "pyproject.toml").write_text(
        """[project]
name = "parallel-fixture"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []
""",
        encoding="utf-8",
    )
    subprocess.run(["uv", "lock", "--directory", str(source)], check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=source, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add dependency fixture"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    disposable = tmp_path / "workers"
    artifacts = tmp_path / "artifacts"
    shared = tmp_path / "cache"
    barrier = tmp_path / "barrier"
    fake = write_fake_acpx(tmp_path / "bin", "barrier_success")
    env = os.environ.copy()
    env["FAKE_ACPX_BEHAVIOR"] = "barrier_success"
    env["FAKE_ACPX_BARRIER_DIR"] = str(barrier)
    env["FAKE_ACPX_BARRIER_EXPECTED"] = "6"

    processes: list[subprocess.Popen[str]] = []
    for index in range(6):
        task_id = f"parallel-{index}"
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "grok_worker",
                    "run",
                    "--source",
                    str(source),
                    "--prompt",
                    "bounded parallel test",
                    "--task-id",
                    task_id,
                    "--disposable-root",
                    str(disposable),
                    "--artifact-root",
                    str(artifacts),
                    "--shared-cache-root",
                    str(shared),
                    "--acpx-bin",
                    str(fake),
                    "--max-workers",
                    "8",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        )

    failures: list[str] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=45)
        if process.returncode != 0:
            failures.append(f"exit={process.returncode}\nstdout={stdout}\nstderr={stderr}")

    assert failures == []
    assert len(list(barrier.iterdir())) == 6
    assert not [path for path in disposable.glob("grok-worker-parallel-*")]
    shared_venvs = list((shared / "venvs").iterdir())
    assert len(shared_venvs) == 1
    assert (shared_venvs[0] / "Scripts" / "python.exe").is_file()
    assert (shared_venvs[0] / ".grok-worker-ready").is_file()
    for index in range(6):
        files = sorted(path.name for path in (artifacts / f"parallel-{index}").iterdir())
        assert files == ["changes.patch", "verification.txt", "worker.log"]
