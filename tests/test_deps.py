"""Shared dependency environments: frozen argv, hard failure, no local env."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from grok_worker.deps import (
    DepsError,
    build_uv_sync_cmd,
    compute_fingerprint,
    detect_clone_local_env,
    prepare_shared_env,
    worker_env_exports,
)
from grok_worker.runner import RunConfig, run_worker
from tests.path_helpers import symlink_or_skip


def _write_pyproject(source: Path, lock_extra: str = "") -> None:
    (source / "pyproject.toml").write_text(
        """[project]
name = "demo"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []
""",
        encoding="utf-8",
    )
    (source / "uv.lock").write_text(f"version = 1\n{lock_extra}\n", encoding="utf-8")


def _fake_uv_sync_factory(calls: list[list[str]]):
    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        env = kwargs.get("env") or {}
        venv = Path(env.get("UV_PROJECT_ENVIRONMENT", ""))
        if venv:
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            py = venv / "bin" / "python"
            if not py.exists():
                py.write_text("#!/bin/sh\n", encoding="utf-8")
                py.chmod(0o755)

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        return R()

    return fake_run


def test_same_inputs_same_fingerprint(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write_pyproject(a)
    _write_pyproject(b)
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_changed_lock_maps_differently(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write_pyproject(a, "pkg = 1")
    _write_pyproject(b, "pkg = 2")
    assert compute_fingerprint(a) != compute_fingerprint(b)


def test_fingerprint_stable_across_ephemeral_executable_symlinks(tmp_path: Path) -> None:
    """Distinct uv-style ephemeral launcher paths resolve to the same interpreter."""
    import os
    import sys

    from grok_worker.deps import compute_fingerprint, interpreter_identity

    source = tmp_path / "src"
    source.mkdir()
    _write_pyproject(source)

    real_py = Path(os.path.realpath(sys.executable))
    link_a = tmp_path / "builds-v0" / ".tmpAAAA" / "bin" / "python"
    link_b = tmp_path / "builds-v0" / ".tmpBBBB" / "bin" / "python"
    for link in (link_a, link_b):
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        symlink_or_skip(link, real_py)

    assert link_a != link_b
    assert os.path.realpath(link_a) == os.path.realpath(link_b) == str(real_py)
    assert interpreter_identity(str(link_a)) == interpreter_identity(str(link_b))
    assert interpreter_identity(str(link_a)) == interpreter_identity(str(real_py))

    with mock.patch("grok_worker.deps.sys.executable", str(link_a)):
        fp_a = compute_fingerprint(source)
    with mock.patch("grok_worker.deps.sys.executable", str(link_b)):
        fp_b = compute_fingerprint(source)
    with mock.patch("grok_worker.deps.sys.executable", str(real_py)):
        fp_real = compute_fingerprint(source)
    assert fp_a == fp_b == fp_real

    # Different real interpreters still produce different fingerprints.
    other_real = tmp_path / "other-python"
    other_real.write_text("#!/bin/sh\n", encoding="utf-8")
    other_real.chmod(0o755)
    other_link = tmp_path / "builds-v0" / ".tmpCCCC" / "bin" / "python"
    other_link.parent.mkdir(parents=True, exist_ok=True)
    symlink_or_skip(other_link, other_real)
    with mock.patch("grok_worker.deps.sys.executable", str(other_link)):
        fp_other = compute_fingerprint(source)
    assert fp_other != fp_a


def test_exact_frozen_argv(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _write_pyproject(src)
    cmd = build_uv_sync_cmd(src, has_lock=True)
    assert cmd[:2] == ["uv", "sync"]
    for flag in ("--frozen", "--all-groups", "--all-extras", "--no-install-project"):
        assert flag in cmd
    assert "--directory" in cmd
    assert not any("editable" in c for c in cmd)


def test_prepare_uses_exact_frozen_once(tmp_path: Path) -> None:
    source, shared = tmp_path / "src", tmp_path / "shared"
    source.mkdir()
    _write_pyproject(source)
    calls: list[list[str]] = []
    with mock.patch("grok_worker.deps.subprocess.run", side_effect=_fake_uv_sync_factory(calls)):
        with mock.patch("grok_worker.deps.which", return_value="/usr/bin/uv"):
            env = prepare_shared_env(source, shared)
    assert len(calls) == 1
    assert "--frozen" in calls[0] and "--no-install-project" in calls[0]
    assert "UV_PROJECT_ENVIRONMENT" in env and str(shared) in env["UV_PROJECT_ENVIRONMENT"]
    assert not (source / ".venv").exists()
    with mock.patch("grok_worker.deps.subprocess.run", side_effect=_fake_uv_sync_factory(calls)):
        with mock.patch("grok_worker.deps.which", return_value="/usr/bin/uv"):
            env2 = prepare_shared_env(source, shared)
    assert len(calls) == 1
    assert env2["UV_PROJECT_ENVIRONMENT"] == env["UV_PROJECT_ENVIRONMENT"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows venv layout")
def test_prepare_accepts_windows_scripts_interpreter(tmp_path: Path) -> None:
    source, shared = tmp_path / "src", tmp_path / "shared"
    source.mkdir()
    _write_pyproject(source)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        env = kwargs.get("env") or {}
        venv = Path(env["UV_PROJECT_ENVIRONMENT"])
        scripts = venv / "Scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "python.exe").write_bytes(b"windows-python")

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        return Result()

    with mock.patch("grok_worker.deps.subprocess.run", side_effect=fake_run):
        with mock.patch("grok_worker.deps.which", return_value="C:\\Tools\\uv.exe"):
            first = prepare_shared_env(source, shared)
            second = prepare_shared_env(source, shared)

    assert len(calls) == 1
    assert first["UV_PROJECT_ENVIRONMENT"] == second["UV_PROJECT_ENVIRONMENT"]


def test_frozen_failure_no_retry(tmp_path: Path) -> None:
    source, shared = tmp_path / "src", tmp_path / "shared"
    source.mkdir()
    _write_pyproject(source)

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        raise __import__("subprocess").CalledProcessError(1, cmd, stderr="lock mismatch")

    with mock.patch("grok_worker.deps.subprocess.run", side_effect=fake_run):
        with mock.patch("grok_worker.deps.which", return_value="/usr/bin/uv"):
            with pytest.raises(DepsError, match="lock mismatch"):
                prepare_shared_env(source, shared)


@pytest.mark.parametrize("failure", [DepsError("sync failed hard"), OSError("cache denied")])
def test_deps_prewarm_failure_warns_and_still_invokes_backend(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    import subprocess

    _write_pyproject(git_source)
    subprocess.run(["git", "add", "-A"], cwd=git_source, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "deps"], cwd=git_source, check=True, capture_output=True)
    never = tmp_roots["root"] / "bin" / ("never-acpx.cmd" if os.name == "nt" else "never-acpx")
    never.parent.mkdir(parents=True, exist_ok=True)
    invoked_marker = tmp_roots["root"] / "acpx-was-invoked"
    if os.name == "nt":
        never.write_text(
            f'@echo off\r\ntype nul > "{invoked_marker}"\r\nexit /b 99\r\n',
            encoding="utf-8",
        )
    else:
        never.write_text(f"#!/bin/sh\ntouch {invoked_marker}\nexit 99\n", encoding="utf-8")
        never.chmod(0o755)

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise failure

    with mock.patch("grok_worker.worker_exec.prepare_shared_env", side_effect=boom):
        outcome = run_worker(
            RunConfig(
                source=git_source,
                prompt="x",
                backend="acp",
                disposable_root=tmp_roots["disposable"],
                artifact_root=tmp_roots["artifacts"],
                shared_cache_root=tmp_roots["shared"],
                acpx_bin=str(never),
                prepare_deps=True,
                task_id="depsfail",
                skip_post_gc=True,
            )
        )
    assert invoked_marker.exists()
    assert outcome.state == "failed"
    assert "deps prepare failed" not in (outcome.message or "")
    assert outcome.artifact_path is not None
    worker_log = Path(outcome.artifact_path, "worker.log").read_text(
        encoding="utf-8"
    )
    assert "dependency prewarm skipped" in worker_log


def test_no_local_env_detection(tmp_path: Path) -> None:
    c = tmp_path / "clone"
    c.mkdir()
    (c / ".venv").mkdir()
    (c / ".venv-test").mkdir()
    found = detect_clone_local_env(c)
    assert ".venv" in found and ".venv-test" in found


def test_worker_env_exports_require_no_sync() -> None:
    text = worker_env_exports(
        {"UV_CACHE_DIR": "/c", "UV_PROJECT_ENVIRONMENT": "/e", "PYTHONPATH": "/p"}
    )
    assert "uv run --no-sync" in text
    assert "already configured in the process environment" in text
    assert "/c" not in text
    assert "/e" not in text
    assert "/p" not in text
    assert text == worker_env_exports(
        {
            "UV_CACHE_DIR": "/different-clone/cache",
            "UV_PROJECT_ENVIRONMENT": "/different-shared-env",
            "PYTHONPATH": "/different-clone",
        }
    )


def test_worker_env_exports_forbid_uv_when_project_environment_is_absent() -> None:
    text = worker_env_exports({"UV_CACHE_DIR": "/c"})

    assert "Dependency preparation is disabled" in text
    assert "Do not run uv, uv run, uv sync, pip" in text
    assert "Always use: uv run --no-sync" not in text
    assert "UV_PROJECT_ENVIRONMENT" not in text


def test_concurrent_preparation_serialized(tmp_path: Path) -> None:
    """Same-fingerprint concurrent prepares: lock serializes; sync exactly once."""
    import threading
    import time

    source, shared = tmp_path / "src", tmp_path / "shared"
    source.mkdir()
    _write_pyproject(source)
    holding, release = threading.Event(), threading.Event()
    entered = max_concurrent = 0
    lock = threading.Lock()
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal entered, max_concurrent
        calls.append(list(cmd))
        env = kwargs.get("env") or {}
        venv = Path(env.get("UV_PROJECT_ENVIRONMENT", ""))
        if venv:
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            py = venv / "bin" / "python"
            if not py.exists():
                py.write_text("#!/bin/sh\n", encoding="utf-8")
                py.chmod(0o755)
        with lock:
            entered += 1
            max_concurrent = max(max_concurrent, entered)
        holding.set()
        assert release.wait(timeout=5)
        with lock:
            entered -= 1

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        return R()

    with mock.patch("grok_worker.deps.subprocess.run", side_effect=fake_run):
        with mock.patch("grok_worker.deps.which", return_value="/usr/bin/uv"):
            t1 = threading.Thread(target=lambda: prepare_shared_env(source, shared))
            t2 = threading.Thread(target=lambda: prepare_shared_env(source, shared))
            t1.start()
            assert holding.wait(timeout=5)
            t2.start()
            time.sleep(0.25)
            assert max_concurrent == 1
            release.set()
            t1.join(timeout=10)
            t2.join(timeout=10)
    assert max_concurrent == 1 and len(calls) == 1


def test_changed_fingerprint_creates_another_env(tmp_path: Path) -> None:
    source, shared = tmp_path / "src", tmp_path / "shared"
    source.mkdir()
    _write_pyproject(source, "pkg = 1")
    calls: list[list[str]] = []
    with mock.patch("grok_worker.deps.subprocess.run", side_effect=_fake_uv_sync_factory(calls)):
        with mock.patch("grok_worker.deps.which", return_value="/usr/bin/uv"):
            e1 = prepare_shared_env(source, shared)
    (source / "uv.lock").write_text("version = 1\npkg = 2\n", encoding="utf-8")
    with mock.patch("grok_worker.deps.subprocess.run", side_effect=_fake_uv_sync_factory(calls)):
        with mock.patch("grok_worker.deps.which", return_value="/usr/bin/uv"):
            e2 = prepare_shared_env(source, shared)
    assert len(calls) == 2
    assert e1["UV_PROJECT_ENVIRONMENT"] != e2["UV_PROJECT_ENVIRONMENT"]
    assert e1["GROK_WORKER_DEPS_FINGERPRINT"] != e2["GROK_WORKER_DEPS_FINGERPRINT"]
