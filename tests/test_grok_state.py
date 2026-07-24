from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from grok_worker.grok_state import cleanup_clone_session_state, clone_session_root
from grok_worker.safety import SafetyError


def _directory_link_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            pytest.skip("directory symlink creation is unavailable")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    if result.returncode != 0:
        pytest.skip("directory junction creation is unavailable")


def test_cleanup_removes_only_exact_clone_session_bucket(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    environ = {"HOME": str(tmp_path / "home")}
    target = clone_session_root(clone, environ)
    sibling = target.parent / "unrelated-session"
    target.mkdir(parents=True)
    sibling.mkdir()
    (target / "transcript.jsonl").write_text("x\n", encoding="utf-8")

    assert cleanup_clone_session_state(clone, environ) is True
    assert not target.exists()
    assert sibling.is_dir()
    assert cleanup_clone_session_state(clone, environ) is False


def test_cleanup_refuses_symlink_session_bucket(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    environ = {"HOME": str(tmp_path / "home")}
    target = clone_session_root(clone, environ)
    outside = tmp_path / "outside"
    outside.mkdir()
    target.parent.mkdir(parents=True)
    _directory_link_or_skip(target, outside)

    with pytest.raises(SafetyError, match="symlink"):
        cleanup_clone_session_state(clone, environ)
    assert outside.is_dir()
