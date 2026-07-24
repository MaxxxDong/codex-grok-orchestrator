"""Dirty snapshot commands must not grow with the safe-path inventory."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import grok_worker.clone as clone_module
from grok_worker.disclosure import DisclosureSummary


def test_dirty_snapshot_keeps_git_argv_bounded(
    git_source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (git_source / "README.md").write_text("changed\n", encoding="utf-8")
    paths = ["README.md", *(f"nested/{index:04d}-{'x' * 60}.txt" for index in range(700))]
    summary = DisclosureSummary(
        source_kind="git",
        included_dirty_paths=paths,
        included_dirty_count=len(paths),
        include_dirty=True,
    )
    monkeypatch.setattr(clone_module, "plan_disclosure", lambda *args, **kwargs: summary)
    original = subprocess.check_output

    def bounded_check_output(command, *args, **kwargs):  # type: ignore[no-untyped-def]
        if command[0] == "git" and "diff" in command:
            assert len(command) < 20, "dirty path inventory leaked into git argv"
        return original(command, *args, **kwargs)

    monkeypatch.setattr(clone_module.subprocess, "check_output", bounded_check_output)
    clone, _base, fingerprint, _disclosure = clone_module.create_workspace(
        git_source, tmp_path / "disposable", "bounded-argv"
    )

    assert fingerprint is not None
    assert (clone / "README.md").read_text(encoding="utf-8") == "changed\n"
