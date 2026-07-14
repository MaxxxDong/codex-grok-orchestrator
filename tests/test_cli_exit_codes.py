"""CLI exit-code regressions: failure paths must return nonzero via main()."""

from __future__ import annotations

from pathlib import Path

from grok_worker.cli import main
from tests.conftest import init_git_repo


def test_invalid_mode_returns_nonzero(tmp_path: Path) -> None:
    """Invalid --mode must exit nonzero through the user-visible main() entry."""
    source = tmp_path / "source"
    init_git_repo(source)
    code = main(
        [
            "run",
            "--source",
            str(source),
            "--prompt",
            "x",
            "--mode",
            "invalid",
            "--disposable-root",
            str(tmp_path / "disp"),
            "--no-prepare-deps",
        ]
    )
    assert code != 0, f"expected nonzero exit for invalid mode, got {code}"


def test_dirty_source_refusal_returns_nonzero(tmp_path: Path) -> None:
    """Dirty source preflight refusal must exit nonzero through main()."""
    source = tmp_path / "source"
    init_git_repo(source)
    (source / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    code = main(
        [
            "run",
            "--source",
            str(source),
            "--prompt",
            "x",
            "--disposable-root",
            str(tmp_path / "disp"),
            "--artifact-root",
            str(tmp_path / "arts"),
            "--shared-cache-root",
            str(tmp_path / "shared"),
            "--no-prepare-deps",
        ]
    )
    assert code != 0, f"expected nonzero exit for dirty source, got {code}"


def test_help_returns_zero() -> None:
    """Normal help path must still exit 0."""
    code = main(["--help"])
    assert code == 0
