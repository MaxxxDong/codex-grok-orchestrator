"""Temporarily hide repository-root MCP discovery inside a disposable clone."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _git(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _skip_worktree_enabled(clone: Path) -> bool:
    result = _git(clone, "ls-files", "-v", "--", ".mcp.json")
    return result.returncode == 0 and result.stdout.startswith("S ")


def _is_tracked(clone: Path) -> bool:
    return _git(clone, "ls-files", "--error-unmatch", "--", ".mcp.json").returncode == 0


def _set_skip_worktree(clone: Path, *, enabled: bool) -> None:
    option = "--skip-worktree" if enabled else "--no-skip-worktree"
    result = _git(clone, "update-index", option, "--", ".mcp.json")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise OSError(f"cannot set project MCP isolation flag: {detail}")


def _quarantine(path: Path, backup_root: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    candidate = backup_root / "project-mcp.worker-replacement"
    index = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = backup_root / f"project-mcp.worker-replacement-{index}"
        index += 1
    os.replace(path, candidate)


def _recover_stale_mask(
    clone: Path,
    source: Path,
    backup: Path,
    state: Path,
    backup_root: Path,
) -> None:
    if not backup.exists() and not backup.is_symlink():
        owned_skip_flag = (
            state.is_file()
            and state.read_text(encoding="utf-8").strip() == "changed_skip"
        )
        if owned_skip_flag and _skip_worktree_enabled(clone):
            _set_skip_worktree(clone, enabled=False)
        state.unlink(missing_ok=True)
        return
    _quarantine(source, backup_root)
    os.replace(backup, source)
    owned_skip_flag = (
        state.is_file()
        and state.read_text(encoding="utf-8").strip() == "changed_skip"
    )
    if owned_skip_flag:
        _set_skip_worktree(clone, enabled=False)
    state.unlink(missing_ok=True)


@contextmanager
def isolate_project_mcp(clone: Path, backup_root: Path) -> Iterator[bool]:
    """Mask .mcp.json without exposing a deletion or losing original bytes.

    The index flag hides the temporary move from Git-aware agents. A state file
    records whether this invocation owns that flag so interrupted runs can
    restore it without changing a user's pre-existing skip-worktree choice.
    """
    source = clone / ".mcp.json"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / "project-mcp.json.masked"
    state = backup_root / "project-mcp-mask-state"
    _recover_stale_mask(clone, source, backup, state, backup_root)

    if not source.is_file() and not source.is_symlink():
        yield False
        return

    changed_skip = _is_tracked(clone) and not _skip_worktree_enabled(clone)
    state.write_text("changed_skip\n" if changed_skip else "preserve_skip\n", encoding="utf-8")
    if changed_skip:
        _set_skip_worktree(clone, enabled=True)
    try:
        os.replace(source, backup)
    except OSError:
        if changed_skip:
            _set_skip_worktree(clone, enabled=False)
        state.unlink(missing_ok=True)
        raise

    try:
        yield True
    finally:
        _quarantine(source, backup_root)
        try:
            os.replace(backup, source)
        finally:
            if changed_skip:
                _set_skip_worktree(clone, enabled=False)
            state.unlink(missing_ok=True)
