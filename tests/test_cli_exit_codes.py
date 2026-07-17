"""CLI exit-code regressions: failure paths must return nonzero via main()."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from grok_worker.cli import main
from grok_worker.run_config import RunConfig
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


def test_run_cli_passes_configurable_worker_limit(tmp_path: Path) -> None:
    seen: list[int] = []

    def fake_run(cfg):  # type: ignore[no-untyped-def]
        seen.append(cfg.max_workers)
        return SimpleNamespace(
            task_id="limit-test",
            state="success",
            exit_code=0,
            clone_path=None,
            artifact_path=str(tmp_path / "artifacts"),
            message="ok",
        )

    with mock.patch("grok_worker.cli_cmds.run_worker", side_effect=fake_run):
        code = main(
            [
                "run",
                "--source",
                str(tmp_path),
                "--prompt",
                "x",
                "--max-workers",
                "24",
                "--no-prepare-deps",
            ]
        )

    assert code == 0
    assert seen == [24]


def test_run_config_rejects_non_positive_worker_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_workers must be at least 1"):
        RunConfig(source=tmp_path, prompt="x", max_workers=0)


def test_session_start_cli_passes_configurable_worker_limit(tmp_path: Path) -> None:
    seen: list[int] = []

    def fake_start(cfg):  # type: ignore[no-untyped-def]
        seen.append(cfg.max_workers)
        return SimpleNamespace(
            task_id="session-limit-test",
            state="session_open",
            prompt_count=1,
            clone_path=str(tmp_path / "clone"),
            artifact_path=None,
        )

    with mock.patch("grok_worker.session_cli.start_session", side_effect=fake_start):
        code = main(
            [
                "session-start",
                "--source",
                str(tmp_path),
                "--manifest-file",
                str(tmp_path / "task.json"),
                "--max-workers",
                "24",
                "--no-prepare-deps",
            ]
        )

    assert code == 0
    assert seen == [24]
