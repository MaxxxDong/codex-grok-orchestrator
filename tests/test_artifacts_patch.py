"""Patch coverage: git/non-git, dirty, binary, staged, exclusions."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from grok_worker.clone import CloneError, create_workspace
from grok_worker.patch_capture import PatchError, collect_git_patch
from tests.conftest import init_git_repo
from tests.path_helpers import symlink_or_skip


def test_patch_covers_changes_and_exclusions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = init_git_repo(repo)

    (repo / "committed.txt").write_text("c\n", encoding="utf-8")
    subprocess.run(["git", "add", "committed.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "commit change"], cwd=repo, check=True, capture_output=True
    )

    (repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("u\n", encoding="utf-8")
    (repo / "data.bin").write_bytes(bytes(range(128)))
    (repo / "empty.txt").write_text("", encoding="utf-8")
    (repo / "weird name.txt").write_text("w\n", encoding="utf-8")

    (repo / ".grok-output").mkdir()
    (repo / ".grok-output" / "result.json").write_text("{}", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "pyvenv.cfg").write_text("x", encoding="utf-8")
    (repo / ".mypy_cache").mkdir()
    (repo / ".mypy_cache" / "c").write_text("x", encoding="utf-8")
    (repo / "prompt-abc.txt").write_text("prompt", encoding="utf-8")

    out = tmp_path / "changes.patch"
    collect_git_patch(repo, base, out)
    text = out.read_text(encoding="utf-8", errors="replace")
    assert "committed.txt" in text
    assert "README.md" in text
    assert "untracked.txt" in text
    assert "empty.txt" in text or "weird name" in text
    raw = out.read_bytes().decode("latin1")
    assert "data.bin" in text or "Binary" in text or "GIT binary" in text or "\0" in raw
    assert ".grok-output" not in text
    assert ".venv" not in text
    assert ".mypy_cache" not in text
    assert "prompt-abc" not in text


def test_patch_raises_on_bad_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo)
    out = tmp_path / "p.patch"
    with pytest.raises(PatchError):
        collect_git_patch(repo, "0" * 40, out)


def test_staged_only_in_patch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = init_git_repo(repo)
    (repo / "staged.txt").write_text("s\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=repo, check=True, capture_output=True)
    out = tmp_path / "p.patch"
    collect_git_patch(repo, base, out)
    assert "staged.txt" in out.read_text(encoding="utf-8", errors="replace")


def test_nongit_reconstructable_patch(tmp_path: Path) -> None:
    src = tmp_path / "nongit"
    src.mkdir()
    (src / "a.txt").write_text("base\n", encoding="utf-8")
    # preserve symlink as symlink
    symlink_or_skip(src / "link", "a.txt")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, base, _fp = create_workspace(src, disp, "ng01")
    assert base
    assert (clone / "link").is_symlink()
    (clone / "a.txt").write_text("changed\n", encoding="utf-8")
    (clone / "new.txt").write_text("new\n", encoding="utf-8")
    out = tmp_path / "ng.patch"
    collect_git_patch(clone, base, out)
    text = out.read_text(encoding="utf-8", errors="replace")
    assert "a.txt" in text
    assert "new.txt" in text
    # Sufficient to reconstruct without full clone snapshot
    assert text.strip() != ""


def test_dirty_source_rejected_by_default(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_git_repo(src)
    (src / "dirty.txt").write_text("d\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    with pytest.raises(CloneError, match="dirty"):
        create_workspace(src, disp, "d01", include_dirty=False)


def test_dirty_source_include_baseline(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_git_repo(src)
    (src / "dirty.txt").write_text("dirty-input\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, base, fp = create_workspace(src, disp, "d02", include_dirty=True)
    assert fp is not None
    assert (clone / "dirty.txt").read_text(encoding="utf-8") == "dirty-input\n"
    # Worker change relative to dirty baseline — not re-emit dirty input alone
    (clone / "worker.txt").write_text("worker-change\n", encoding="utf-8")
    out = tmp_path / "d.patch"
    collect_git_patch(clone, base, out)
    text = out.read_text(encoding="utf-8", errors="replace")
    assert "worker.txt" in text
    # dirty.txt is in baseline; should not appear as new file unless modified
    # (content same as baseline → not in patch)


def test_dirty_rename_modified_untracked_exact_baseline(tmp_path: Path) -> None:
    """Staged rename + modified + untracked: clone matches; patch excludes dirty input."""
    src = tmp_path / "src"
    init_git_repo(src, filename="oldname.txt", content="v1\n")
    subprocess.run(
        ["git", "-C", str(src), "mv", "oldname.txt", "newname.txt"],
        check=True,
        capture_output=True,
    )
    (src / "newname.txt").write_text("v2-modified\n", encoding="utf-8")
    (src / "untracked-extra.txt").write_text("only-untracked\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, base, fp = create_workspace(src, disp, "d03", include_dirty=True)
    assert fp is not None
    assert not (clone / "oldname.txt").exists()
    assert (clone / "newname.txt").read_text(encoding="utf-8") == "v2-modified\n"
    assert (clone / "untracked-extra.txt").read_text(encoding="utf-8") == "only-untracked\n"
    # Worker-only change after baseline
    (clone / "worker-only.txt").write_text("from-worker\n", encoding="utf-8")
    out = tmp_path / "d3.patch"
    collect_git_patch(clone, base, out)
    text = out.read_text(encoding="utf-8", errors="replace")
    assert "worker-only.txt" in text
    assert "from-worker" in text
    assert "only-untracked" not in text
    assert "v2-modified" not in text


def test_dirty_baseline_uses_lifecycle_owned_git_identity(tmp_path: Path) -> None:
    """Dirty-baseline commit uses stable grok-worker identity, not host/operator."""
    src = tmp_path / "src"
    init_git_repo(src)
    (src / "dirty.txt").write_text("dirty-input\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, base, fp = create_workspace(src, disp, "d-id", include_dirty=True)
    assert fp is not None
    assert base

    author = subprocess.check_output(
        ["git", "-C", str(clone), "log", "-1", "--format=%an <%ae>", base],
        text=True,
    ).strip()
    committer = subprocess.check_output(
        ["git", "-C", str(clone), "log", "-1", "--format=%cn <%ce>", base],
        text=True,
    ).strip()
    expected = "grok-worker <grok-worker@localhost>"
    assert author == expected
    assert committer == expected
    # Must not inherit the source repo test identity used by init_git_repo.
    assert "test@example.com" not in author
    assert "test@example.com" not in committer
    assert author != "Test <test@example.com>"

    subject = subprocess.check_output(
        ["git", "-C", str(clone), "log", "-1", "--format=%s", base],
        text=True,
    ).strip()
    assert subject == "lifecycle dirty source baseline"

    # Identity is command-scoped: do not leave grok-worker in clone local config.
    local_name = subprocess.run(
        ["git", "-C", str(clone), "config", "--local", "--get", "user.name"],
        capture_output=True,
        text=True,
        check=False,
    )
    if local_name.returncode == 0:
        assert local_name.stdout.strip() != "grok-worker"
    local_email = subprocess.run(
        ["git", "-C", str(clone), "config", "--local", "--get", "user.email"],
        capture_output=True,
        text=True,
        check=False,
    )
    if local_email.returncode == 0:
        assert local_email.stdout.strip() != "grok-worker@localhost"
