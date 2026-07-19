"""Prompt-only research mode, timeout constants, and health diagnostic policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from grok_worker.activity_lease import read_lease
from grok_worker.cli import main
from grok_worker.constants import (
    DEFAULT_ACPX_TIMEOUT,
    DEFAULT_HARD_TIMEOUT,
    HEALTH_INSPECT_INTERVAL_SECONDS,
    LONG_TASK_TIMEOUT,
    PROMPT_ONLY_SOURCE,
)
from grok_worker.health import collect_health, inspect_clone_health
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import meta_dir, meta_path
from grok_worker.prompt_cache import build_one_shot_prompt
from grok_worker.run_config import RunConfig, build_acpx_cmd
from grok_worker.runner import run_worker
from grok_worker.session_process import SessionConfig


def test_default_task_timeout_1800_constant_aligned() -> None:
    assert DEFAULT_ACPX_TIMEOUT == 1800
    default = RunConfig(source=Path("."), prompt="x")
    assert default.timeout == DEFAULT_ACPX_TIMEOUT
    assert default.backend == "native"
    fields = {f.name: f for f in SessionConfig.__dataclass_fields__.values()}
    assert fields["timeout"].default is DEFAULT_ACPX_TIMEOUT


def test_extended_timeout_3600_supported(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
) -> None:
    assert LONG_TASK_TIMEOUT == 3600
    cfg = RunConfig(
        source=git_source,
        prompt="long task",
        backend="acp",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        timeout=LONG_TASK_TIMEOUT,
        task_id="long-to",
        skip_post_gc=True,
        keep_reason="inspect lease",
        mode="analysis",
    )
    outcome = run_worker(cfg)
    assert outcome.exit_code == 0
    assert outcome.clone_path is not None
    lease = read_lease(Path(outcome.clone_path))
    assert lease.idle_timeout_seconds == LONG_TASK_TIMEOUT
    assert lease.hard_timeout_seconds == DEFAULT_HARD_TIMEOUT
    cmd = build_acpx_cmd(cfg, git_source, "agent", "p")
    assert "--timeout" not in cmd


def test_ordinary_timeout_is_worker_lease_not_acpx_deadline(tmp_path: Path) -> None:
    cfg = RunConfig(
        source=tmp_path, prompt="p", backend="acp", timeout=DEFAULT_ACPX_TIMEOUT
    )
    cmd = build_acpx_cmd(cfg, tmp_path, "agent", "hello")
    assert "--timeout" not in cmd


def test_prompt_only_research_mode(
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
) -> None:
    cfg = RunConfig(
        source=None,
        prompt="Research a design question without a repository.",
        backend="acp",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        prompt_only=True,
        mode="research",
        task_id="po-research",
        skip_post_gc=True,
    )
    outcome = run_worker(cfg)
    assert outcome.exit_code == 0
    # Clone may be deleted on success; if present, source must be prompt-only.
    clones = list(tmp_roots["disposable"].glob("grok-worker-*"))
    if clones:
        meta = WorkerMeta.read(meta_path(clones[0]))
        assert meta.source_realpath == PROMPT_ONLY_SOURCE


def test_prompt_only_research_auto_approves_non_terminal_search_tools(tmp_path: Path) -> None:
    """Prompt-only research may search externally without exposing a terminal."""
    cfg = RunConfig(
        source=None,
        prompt="Research current public evidence.",
        backend="acp",
        prompt_only=True,
        mode="research",
    )

    command = build_acpx_cmd(cfg, tmp_path, "agent", "prompt")

    assert "--approve-all" in command
    assert "--no-terminal" in command
    assert "--approve-reads" not in command
    assert "--non-interactive-permissions" not in command


def test_prompt_only_rejects_implementation() -> None:
    from grok_worker.clone import CloneError

    cfg = RunConfig(
        source=None,
        prompt="x",
        backend="acp",
        prompt_only=True,
        mode="implementation",
        prepare_deps=False,
    )
    with pytest.raises(CloneError, match="prompt-only"):
        run_worker(cfg)


def test_prompt_only_rejects_dirty_flags(tmp_path: Path) -> None:
    from grok_worker.clone import CloneError

    cfg = RunConfig(
        source=None,
        prompt="x",
        backend="acp",
        prompt_only=True,
        mode="research",
        include_dirty=True,
        prepare_deps=False,
        disposable_root=tmp_path / "d",
        artifact_root=tmp_path / "a",
        shared_cache_root=tmp_path / "s",
    )
    with pytest.raises(CloneError, match="dirty|prompt-only"):
        run_worker(cfg)


def test_prompt_only_rejects_non_null_source(tmp_path: Path) -> None:
    """Library/API path must reject prompt_only=True with source set (like CLI)."""
    from grok_worker.clone import CloneError

    cfg = RunConfig(
        source=tmp_path / "repo",
        prompt="x",
        backend="acp",
        prompt_only=True,
        mode="research",
        prepare_deps=False,
        disposable_root=tmp_path / "d",
        artifact_root=tmp_path / "a",
        shared_cache_root=tmp_path / "s",
    )
    with pytest.raises(CloneError, match="prompt-only|source"):
        run_worker(cfg)


def test_disclosure_summary_on_lifecycle_meta(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
) -> None:
    cfg = RunConfig(
        source=git_source,
        prompt="meta disclosure",
        backend="acp",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        mode="analysis",
        task_id="disc-meta",
        skip_post_gc=True,
        keep_reason="inspect-meta",
    )
    outcome = run_worker(cfg)
    assert outcome.exit_code == 0
    assert outcome.clone_path is not None
    meta = WorkerMeta.read(meta_path(Path(outcome.clone_path)))
    assert meta.disclosure_summary is not None
    assert meta.disclosure_summary.get("source_kind") == "git"
    assert meta.disclosure_summary.get("risk_decision") == "allow"
    # Values/content/prompt/env-free.
    blob = str(meta.disclosure_summary)
    assert "meta disclosure" not in blob
    assert "PATH=" not in blob


def test_one_shot_research_prompt_maps_to_research_role() -> None:
    prompt = build_one_shot_prompt(None, "research", "Investigate X")
    assert "Role: research" in prompt
    assert "Role: implement" not in prompt


def test_health_interval_300_diagnostic_only(
    tmp_roots: dict[str, Path],
) -> None:
    assert HEALTH_INSPECT_INTERVAL_SECONDS == 300
    clone = tmp_roots["disposable"] / "h1"
    clone.mkdir()
    meta_dir(clone).mkdir(parents=True)
    now = utc_now()
    meta = WorkerMeta(
        schema_version=1,
        task_id="h1",
        source_realpath="/tmp/s",
        clone_realpath=str(clone.resolve()),
        state=WorkerState.RUNNING,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by="grok-worker-lifecycle",
        timeout_seconds=1800,
        run_id="run-h1",
    )
    meta.write(meta_path(clone))
    before = meta_path(clone).read_text(encoding="utf-8")
    row = inspect_clone_health(meta, clone)
    assert row["diagnostic_only"] is True
    assert row["health_interval_seconds"] == 300
    assert row["result_ready"] is False
    assert row["artifact_ready"] is False
    assert row["progress_step"] is None
    assert row["activity_source"] in {"lifecycle", "workspace"}
    assert meta_path(clone).read_text(encoding="utf-8") == before
    report = collect_health(tmp_roots["disposable"])
    assert report.mutates_worker is False
    assert report.diagnostic_only is True
    assert report.interval_seconds == 300


def test_health_cli_json(
    tmp_roots: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "health",
            "--disposable-root",
            str(tmp_roots["disposable"]),
            "--json",
        ]
    )
    assert code == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostic_only"] is True
    assert payload["interval_seconds"] == 300
    assert payload["mutates_worker"] is False
