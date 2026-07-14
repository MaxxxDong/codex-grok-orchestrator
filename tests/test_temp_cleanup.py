"""Stale tmp cleanup: age, symlink skip, non-direct children."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from grok_worker.temp_cleanup import clean_stale_tmp


def test_temp_cleanup_old_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp = tmp_path / "t"
    tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    old = tmp / "grok-olddir"
    old.mkdir()
    (old / "f").write_text("x", encoding="utf-8")
    os.utime(old, (1, 1))
    young = tmp / "grok-youngdir"
    young.mkdir()
    removed = clean_stale_tmp(age_hours=1.0, now=time.time())
    assert any("grok-olddir" in r for r in removed)
    assert not old.exists()
    assert young.exists()


def test_temp_cleanup_skips_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp = tmp_path / "t"
    tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    real = tmp_path / "real"
    real.mkdir()
    (real / "s").write_text("s", encoding="utf-8")
    link = tmp / "grok-link"
    link.symlink_to(real)
    os.utime(real, (1, 1), follow_symlinks=False)
    removed = clean_stale_tmp(age_hours=0.0, now=time.time())
    assert not any("grok-link" in r for r in removed)
    assert (real / "s").is_file()
