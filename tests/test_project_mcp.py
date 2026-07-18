"""Project MCP masking preserves source bytes and a clean Git view."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from grok_worker.project_mcp import isolate_project_mcp


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True
    ).strip()


def test_tracked_project_mcp_is_hidden_from_git_and_restored(
    git_source: Path, tmp_roots: dict[str, Path]
) -> None:
    source = git_source / ".mcp.json"
    original = b'{"mcpServers":{"project":{"command":"one"}}}\n'
    source.write_bytes(original)
    subprocess.run(["git", "-C", str(git_source), "add", ".mcp.json"], check=True)
    subprocess.run(
        ["git", "-C", str(git_source), "commit", "-m", "add project mcp"],
        check=True,
        capture_output=True,
    )

    with isolate_project_mcp(git_source, tmp_roots["shared"]) as masked:
        assert masked is True
        assert not source.exists()
        assert _git(git_source, "status", "--porcelain") == ""
        assert _git(git_source, "ls-files", "-v", "--", ".mcp.json").startswith("S ")

    assert source.read_bytes() == original
    assert _git(git_source, "status", "--porcelain") == ""
    assert not _git(git_source, "ls-files", "-v", "--", ".mcp.json").startswith("S ")


def test_dirty_tracked_original_wins_over_worker_replacement(
    git_source: Path, tmp_roots: dict[str, Path]
) -> None:
    source = git_source / ".mcp.json"
    source.write_text('{"version":"committed"}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(git_source), "add", ".mcp.json"], check=True)
    subprocess.run(
        ["git", "-C", str(git_source), "commit", "-m", "add project mcp"],
        check=True,
        capture_output=True,
    )
    dirty = b'{"version":"user-dirty"}\n'
    source.write_bytes(dirty)

    with isolate_project_mcp(git_source, tmp_roots["shared"]):
        source.write_text('{"version":"worker"}\n', encoding="utf-8")

    assert source.read_bytes() == dirty
    replacements = list(tmp_roots["shared"].glob("project-mcp.worker-replacement*"))
    assert len(replacements) == 1
    assert replacements[0].read_text(encoding="utf-8") == '{"version":"worker"}\n'
    assert _git(git_source, "status", "--porcelain") == "M .mcp.json"


def test_untracked_project_mcp_is_restored_without_index_changes(
    git_source: Path, tmp_roots: dict[str, Path]
) -> None:
    source = git_source / ".mcp.json"
    original = b'{"untracked":true}\n'
    source.write_bytes(original)

    with isolate_project_mcp(git_source, tmp_roots["shared"]):
        assert not source.exists()

    assert source.read_bytes() == original
    assert _git(git_source, "status", "--porcelain") == "?? .mcp.json"


def test_stale_mask_is_recovered_before_next_invocation(
    git_source: Path, tmp_roots: dict[str, Path]
) -> None:
    source = git_source / ".mcp.json"
    original = b'{"version":"original"}\n'
    source.write_bytes(original)
    subprocess.run(["git", "-C", str(git_source), "add", ".mcp.json"], check=True)
    subprocess.run(
        ["git", "-C", str(git_source), "commit", "-m", "add project mcp"],
        check=True,
        capture_output=True,
    )
    backup_root = tmp_roots["shared"]
    backup_root.mkdir(parents=True, exist_ok=True)
    source.replace(backup_root / "project-mcp.json.masked")
    (backup_root / "project-mcp-mask-state").write_text(
        "changed_skip\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(git_source), "update-index", "--skip-worktree", ".mcp.json"],
        check=True,
    )

    with isolate_project_mcp(git_source, backup_root):
        assert not source.exists()

    assert source.read_bytes() == original
    assert not _git(git_source, "ls-files", "-v", "--", ".mcp.json").startswith("S ")


def test_stale_owned_skip_without_backup_is_recovered(
    git_source: Path, tmp_roots: dict[str, Path]
) -> None:
    source = git_source / ".mcp.json"
    source.write_text('{"version":"original"}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(git_source), "add", ".mcp.json"], check=True)
    subprocess.run(
        ["git", "-C", str(git_source), "commit", "-m", "add project mcp"],
        check=True,
        capture_output=True,
    )
    backup_root = tmp_roots["shared"]
    (backup_root / "project-mcp-mask-state").write_text(
        "changed_skip\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(git_source), "update-index", "--skip-worktree", ".mcp.json"],
        check=True,
    )

    with isolate_project_mcp(git_source, backup_root):
        assert not source.exists()

    assert source.read_text(encoding="utf-8") == '{"version":"original"}\n'
    assert not _git(git_source, "ls-files", "-v", "--", ".mcp.json").startswith("S ")


def test_tracked_internal_symlink_project_mcp_is_masked_and_restored(
    git_source: Path, tmp_roots: dict[str, Path]
) -> None:
    target = git_source / "project-mcp-target.json"
    target.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    source = git_source / ".mcp.json"
    try:
        source.symlink_to(target.name)
    except OSError:
        pytest.skip("file symlink creation is unavailable for this Windows token")
    subprocess.run(
        ["git", "-C", str(git_source), "add", ".mcp.json", target.name], check=True
    )
    subprocess.run(
        ["git", "-C", str(git_source), "commit", "-m", "add linked project mcp"],
        check=True,
        capture_output=True,
    )

    with isolate_project_mcp(git_source, tmp_roots["shared"]) as masked:
        assert masked is True
        assert not source.exists() and not source.is_symlink()
        assert _git(git_source, "status", "--porcelain") == ""

    assert source.is_symlink()
    assert source.readlink() == Path(target.name)
    assert _git(git_source, "status", "--porcelain") == ""


def test_preexisting_skip_worktree_flag_is_preserved(
    git_source: Path, tmp_roots: dict[str, Path]
) -> None:
    source = git_source / ".mcp.json"
    original = b'{"version":"original"}\n'
    source.write_bytes(original)
    subprocess.run(["git", "-C", str(git_source), "add", ".mcp.json"], check=True)
    subprocess.run(
        ["git", "-C", str(git_source), "commit", "-m", "add project mcp"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_source), "update-index", "--skip-worktree", ".mcp.json"],
        check=True,
    )

    with isolate_project_mcp(git_source, tmp_roots["shared"]):
        assert not source.exists()

    assert source.read_bytes() == original
    assert _git(git_source, "ls-files", "-v", "--", ".mcp.json").startswith("S ")
