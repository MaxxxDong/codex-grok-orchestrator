"""Shared fixtures: temp dirs, fake git source, fake acpx."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fake_acpx import write_fake_acpx


@pytest.fixture
def tmp_roots(tmp_path: Path) -> dict[str, Path]:
    source = tmp_path / "source"
    disposable = tmp_path / "disposable"
    artifacts = tmp_path / "artifacts"
    shared = tmp_path / "shared-cache"
    for p in (source, disposable, artifacts, shared):
        p.mkdir(parents=True)
    return {
        "source": source,
        "disposable": disposable,
        "artifacts": artifacts,
        "shared": shared,
        "root": tmp_path,
    }


def init_git_repo(path: Path, *, filename: str = "README.md", content: str = "hello\n") -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()
    return head


@pytest.fixture
def git_source(tmp_roots: dict[str, Path]) -> Path:
    init_git_repo(tmp_roots["source"])
    return tmp_roots["source"]


@pytest.fixture
def short_tmp_roots() -> Iterator[dict[str, Path]]:
    """Use a shallow root for tests whose URL-encoded Windows session path matters."""
    with tempfile.TemporaryDirectory(prefix="gw-test-") as raw_root:
        root = Path(raw_root)
        roots = {
            "source": root / "s",
            "disposable": root / "d",
            "artifacts": root / "a",
            "shared": root / "c",
            "root": root,
        }
        for path in roots.values():
            path.mkdir(parents=True, exist_ok=True)
        yield roots


@pytest.fixture
def short_git_source(short_tmp_roots: dict[str, Path]) -> Path:
    init_git_repo(short_tmp_roots["source"])
    return short_tmp_roots["source"]


@pytest.fixture
def fake_acpx_success(tmp_roots: dict[str, Path]) -> Path:
    return write_fake_acpx(tmp_roots["root"] / "bin", "success")


@pytest.fixture
def path_with_fake_acpx(fake_acpx_success: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = fake_acpx_success.parent
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return fake_acpx_success
