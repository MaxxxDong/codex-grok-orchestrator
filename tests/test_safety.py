"""Deletion safety: symlink TOCTOU, protected paths, task-id traversal, temp."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from grok_worker.safety import SafetyError, safe_rmtree, safe_unlink
from grok_worker.task_id import TaskIdError, validate_task_id
from grok_worker.temp_cleanup import clean_stale_tmp


def test_task_id_traversal_rejected() -> None:
    for bad in ("../x", "a/b", ".hidden", "", "a" * 100, "foo/../bar", "x\\y"):
        with pytest.raises(TaskIdError):
            validate_task_id(bad)
    assert validate_task_id("good-id_1.2") == "good-id_1.2"


def test_output_symlink_escape_rejected(tmp_path: Path) -> None:
    """Reject .grok-output or nested verification directory symlink escapes."""
    from grok_worker.result_schema import (
        ResultError,
        load_valid_result,
        validate_verification_files,
    )

    clone = tmp_path / "clone"
    clone.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.json").write_text(
        '{"schema_version":1,"task_completed":true,"status":"completed",'
        '"summary":"ok","findings":[],"verification":[]}',
        encoding="utf-8",
    )
    (clone / ".grok-output").symlink_to(outside)
    with pytest.raises(ResultError, match="symlink"):
        load_valid_result(clone)

    clone2 = tmp_path / "clone2"
    clone2.mkdir()
    out_dir = clone2 / ".grok-output"
    out_dir.mkdir()
    (out_dir / "result.json").write_text(
        '{"schema_version":1,"task_completed":true,"status":"completed",'
        '"summary":"ok","findings":[],"verification":['
        '{"command":"t","exit_code":0,'
        '"log_path":".grok-output/verification/t.txt"}]}',
        encoding="utf-8",
    )
    victim = tmp_path / "victim-ver"
    victim.mkdir()
    (victim / "t.txt").write_text("log", encoding="utf-8")
    (out_dir / "verification").symlink_to(victim)
    result = load_valid_result(clone2)
    with pytest.raises(ResultError, match="symlink"):
        validate_verification_files(clone2, result)


def test_safe_rmtree_refuses_symlink_clone(tmp_path: Path) -> None:
    root = tmp_path / "disp"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "secret").write_text("s", encoding="utf-8")
    link = root / "clone"
    link.symlink_to(victim)
    with pytest.raises(SafetyError):
        safe_rmtree(link, disposable_root=root, protected=[])
    assert (victim / "secret").is_file()


def test_safe_rmtree_nested_symlink_no_follow(tmp_path: Path) -> None:
    root = tmp_path / "disp"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k", encoding="utf-8")
    clone = root / "c1"
    clone.mkdir()
    (clone / "inner").mkdir()
    (clone / "inner" / "link").symlink_to(outside)
    (clone / "filelink").symlink_to(outside / "keep")
    safe_rmtree(clone, disposable_root=root, protected=[])
    assert (outside / "keep").is_file()
    assert not clone.exists()


def test_protected_paths_refused(tmp_path: Path) -> None:
    root = tmp_path / "disp"
    root.mkdir()
    prot = tmp_path / "artifacts"
    prot.mkdir()
    # try to delete disposable root itself
    with pytest.raises(SafetyError):
        safe_rmtree(root, disposable_root=root, protected=[prot])
    # non-direct child
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    with pytest.raises(SafetyError):
        safe_rmtree(nested, disposable_root=root, protected=[])


def test_top_level_symlink_swap_toctou(tmp_path: Path) -> None:
    root = tmp_path / "disp"
    root.mkdir()
    clone = root / "c2"
    clone.mkdir()
    (clone / "f").write_text("x", encoding="utf-8")
    victim = tmp_path / "homeish"
    victim.mkdir()
    (victim / "secret").write_text("s", encoding="utf-8")

    real_assert = __import__("grok_worker.safety", fromlist=["assert_safe_delete_target"])

    original = real_assert.assert_safe_delete_target

    def swap_then_assert(target, **kwargs):  # type: ignore[no-untyped-def]
        result = original(target, **kwargs)
        # After validation, swap directory for symlink
        import shutil

        shutil.rmtree(clone)
        clone.symlink_to(victim)
        return result

    with mock.patch("grok_worker.safety.assert_safe_delete_target", side_effect=swap_then_assert):
        with pytest.raises(SafetyError):
            safe_rmtree(clone, disposable_root=root, protected=[])
    assert (victim / "secret").is_file()


def test_temp_guarded_file_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    old = tmp / "grok-old.sock"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (0, 0))
    young = tmp / "grok-young.sock"
    young.write_text("y", encoding="utf-8")
    # nested non-direct — should not be touched
    nested = tmp / "sub" / "grok-nested"
    nested.parent.mkdir()
    nested.write_text("n", encoding="utf-8")
    os.utime(nested, (0, 0))
    removed = clean_stale_tmp(age_hours=1.0, now=10_000_000.0)
    assert any("grok-old.sock" in r for r in removed)
    assert not old.exists()
    assert young.exists()
    assert nested.exists()


def test_safe_unlink_file(tmp_path: Path) -> None:
    root = tmp_path / "disp"
    root.mkdir()
    f = root / "grok-x"
    f.write_text("z", encoding="utf-8")
    safe_unlink(f, disposable_root=root, protected=[])
    assert not f.exists()
