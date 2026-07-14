"""Unmarked legacy preservation and archive-before-expire."""

from __future__ import annotations

from pathlib import Path

import pytest

from grok_worker.gc import gc_disposable_root
from grok_worker.legacy import LegacyClass, LegacyError, import_legacy, list_unmarked
from grok_worker.models import WorkerState
from grok_worker.paths import meta_path
from tests.conftest import init_git_repo


def test_unmarked_legacy_not_deleted_by_ordinary_gc(tmp_roots: dict[str, Path]) -> None:
    legacy = tmp_roots["disposable"] / "legacy-clone-a"
    legacy.mkdir()
    (legacy / "data").write_text("important", encoding="utf-8")
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert "legacy-clone-a" in report.skipped_legacy
    assert legacy.is_dir()
    assert list_unmarked(tmp_roots["disposable"]) == ["legacy-clone-a"]


def test_import_legacy_keep(tmp_roots: dict[str, Path]) -> None:
    legacy = tmp_roots["disposable"] / "legacy-keep"
    legacy.mkdir()
    (legacy / "data").write_text("k", encoding="utf-8")
    meta = import_legacy(
        tmp_roots["disposable"],
        "legacy-keep",
        LegacyClass.KEEP,
        reason="historical",
    )
    assert meta.state == WorkerState.KEEP
    assert meta_path(legacy).is_file()
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert "legacy-keep" not in report.removed
    assert legacy.is_dir()


def test_import_legacy_expire_requires_confirm(tmp_roots: dict[str, Path]) -> None:
    legacy = tmp_roots["disposable"] / "legacy-exp"
    init_git_repo(legacy)
    with pytest.raises(LegacyError, match="confirm-expire"):
        import_legacy(
            tmp_roots["disposable"],
            "legacy-exp",
            LegacyClass.EXPIRE,
            reason="cleanup",
            artifact_root=tmp_roots["artifacts"],
            confirm_expire=False,
        )
    assert not meta_path(legacy).is_file()  # still unmarked


def test_import_legacy_expire_after_archive(tmp_roots: dict[str, Path]) -> None:
    legacy = tmp_roots["disposable"] / "legacy-ok"
    base = init_git_repo(legacy)
    (legacy / "extra.txt").write_text("e\n", encoding="utf-8")
    meta = import_legacy(
        tmp_roots["disposable"],
        "legacy-ok",
        LegacyClass.EXPIRE,
        reason="cleanup done",
        artifact_root=tmp_roots["artifacts"],
        confirm_expire=True,
        base_commit=base,
    )
    assert meta.artifact_complete
    assert meta.artifact_path
    assert Path(meta.artifact_path).is_dir()
    assert sorted(path.name for path in Path(meta.artifact_path).iterdir()) == [
        "changes.patch",
        "verification.txt",
        "worker.log",
    ]
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert "legacy-ok" in report.removed
    assert not legacy.exists()


def test_legacy_local_commit_appears_in_patch(tmp_roots: dict[str, Path]) -> None:
    """Local commits since upstream base must appear in the legacy archive patch."""
    import subprocess

    legacy = tmp_roots["disposable"] / "legacy-local"
    init_git_repo(legacy)
    remote = tmp_roots["root"] / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(legacy), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(legacy), "push", "-u", "origin", "HEAD"],
        check=True,
        capture_output=True,
    )
    (legacy / "local-only.txt").write_text("local-commit\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(legacy), "add", "local-only.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(legacy), "commit", "-m", "local"],
        check=True,
        capture_output=True,
    )
    meta = import_legacy(
        tmp_roots["disposable"],
        "legacy-local",
        LegacyClass.EXPIRE,
        reason="archive local commits",
        artifact_root=tmp_roots["artifacts"],
        confirm_expire=True,
    )
    patch = (Path(meta.artifact_path or "") / "changes.patch").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "local-only.txt" in patch
    assert "local-commit" in patch


def test_legacy_git_no_upstream_requires_base(tmp_roots: dict[str, Path]) -> None:
    legacy = tmp_roots["disposable"] / "legacy-noup"
    init_git_repo(legacy)
    (legacy / "x.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(LegacyError, match="base-commit|upstream|origin/HEAD"):
        import_legacy(
            tmp_roots["disposable"],
            "legacy-noup",
            LegacyClass.EXPIRE,
            reason="cleanup",
            artifact_root=tmp_roots["artifacts"],
            confirm_expire=True,
        )
    assert not meta_path(legacy).is_file()
    assert legacy.is_dir()


def test_legacy_nongit_destructive_refuses_unmarked(tmp_roots: dict[str, Path]) -> None:
    legacy = tmp_roots["disposable"] / "legacy-nongit"
    legacy.mkdir()
    (legacy / "data").write_text("important", encoding="utf-8")
    with pytest.raises(LegacyError, match="non-Git|baseline|reviewed"):
        import_legacy(
            tmp_roots["disposable"],
            "legacy-nongit",
            LegacyClass.EXPIRE,
            reason="cleanup",
            artifact_root=tmp_roots["artifacts"],
            confirm_expire=True,
        )
    assert not meta_path(legacy).is_file()
    assert legacy.is_dir()
    assert (legacy / "data").read_text(encoding="utf-8") == "important"
    with pytest.raises(LegacyError, match="non-Git|baseline|reviewed"):
        import_legacy(
            tmp_roots["disposable"],
            "legacy-nongit",
            LegacyClass.RETAIN_24H,
            reason="try retain",
            artifact_root=tmp_roots["artifacts"],
        )
    assert not meta_path(legacy).is_file()


def test_failed_archive_refuses_classification(tmp_roots: dict[str, Path]) -> None:
    legacy = tmp_roots["disposable"] / "legacy-bad"
    base = init_git_repo(legacy)
    pre = tmp_roots["artifacts"] / "legacy-bad"
    pre.mkdir()
    (pre / "blocker").write_text("x", encoding="utf-8")
    with pytest.raises(LegacyError, match="archive failed|refusing|preexisting"):
        import_legacy(
            tmp_roots["disposable"],
            "legacy-bad",
            LegacyClass.RETAIN_24H,
            reason="try",
            artifact_root=tmp_roots["artifacts"],
            base_commit=base,
        )
    assert not meta_path(legacy).is_file()
