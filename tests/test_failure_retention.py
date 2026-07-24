"""Failure retention (24h), partial/failed verification, missing result."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from grok_worker.gc import gc_disposable_root
from grok_worker.models import WorkerMeta
from grok_worker.paths import meta_path
from grok_worker.runner import RunConfig, run_worker
from tests.fake_acpx import write_fake_acpx


def _run(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    behavior: str,
    task_id: str,
    **kwargs: object,
) -> object:
    fake = write_fake_acpx(tmp_roots["root"] / "bin", behavior)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_ACPX_BEHAVIOR", behavior)
    cfg = RunConfig(
        source=git_source,
        prompt="x",
        backend="acp",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(fake),
        prepare_deps=False,
        task_id=task_id,
        failure_retain_hours=24,
        **kwargs,  # type: ignore[arg-type]
    )
    return run_worker(cfg)


def _assert_retained_24h(outcome: object, tmp_roots: dict[str, Path]) -> Path:
    assert outcome.exit_code != 0  # type: ignore[attr-defined]
    assert outcome.state == "failed"  # type: ignore[attr-defined]
    assert outcome.clone_path is not None  # type: ignore[attr-defined]
    clone = Path(outcome.clone_path)  # type: ignore[attr-defined]
    assert clone.is_dir()
    meta = WorkerMeta.read(meta_path(clone))
    assert meta.retention_deadline is not None
    dl = datetime.fromisoformat(meta.retention_deadline)
    if dl.tzinfo is None:
        dl = dl.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    delta = dl - now
    assert timedelta(hours=23) < delta <= timedelta(hours=24, minutes=5)
    return clone


def test_failure_retains_24h_and_expired_gc(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run(git_source, tmp_roots, monkeypatch, "failure", "fail01")
    clone = _assert_retained_24h(outcome, tmp_roots)
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert clone.name not in report.removed
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    meta = WorkerMeta.read(meta_path(clone))
    meta.retention_deadline = past
    meta.write(meta_path(clone))
    report2 = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert clone.name in report2.removed
    assert not clone.exists()


def test_acpx_zero_without_result_is_failure(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run(git_source, tmp_roots, monkeypatch, "acpx_zero_no_result", "nores1")
    _assert_retained_24h(outcome, tmp_roots)


def test_partial_status_is_failure(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run(git_source, tmp_roots, monkeypatch, "partial", "part01")
    _assert_retained_24h(outcome, tmp_roots)


def test_failed_verification_retains(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run(git_source, tmp_roots, monkeypatch, "failed_verify", "verfail")
    _assert_retained_24h(outcome, tmp_roots)


def test_missing_verification_log_retains(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run(git_source, tmp_roots, monkeypatch, "missing_verify_log", "misslog")
    _assert_retained_24h(outcome, tmp_roots)


def test_acpx_nonzero_with_completed_result_retains(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run(git_source, tmp_roots, monkeypatch, "nonzero_completed", "nzcomp")
    _assert_retained_24h(outcome, tmp_roots)


def test_implementation_requires_verification(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run(
        git_source, tmp_roots, monkeypatch, "success_no_verify", "novfy", mode="implementation"
    )
    _assert_retained_24h(outcome, tmp_roots)


def test_analysis_empty_verification_succeeds(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run(
        git_source, tmp_roots, monkeypatch, "success_analysis", "anal01", mode="analysis"
    )
    assert outcome.exit_code == 0  # type: ignore[attr-defined]
    assert outcome.state == "success"  # type: ignore[attr-defined]
    assert outcome.clone_path is None  # type: ignore[attr-defined]


def test_read_only_analysis_can_finalize_from_nonempty_agent_log(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run(
        git_source,
        tmp_roots,
        monkeypatch,
        "acpx_zero_no_result",
        "anal-log",
        mode="analysis",
    )
    assert outcome.exit_code == 0  # type: ignore[attr-defined]
    assert outcome.state == "success"  # type: ignore[attr-defined]
    assert outcome.clone_path is None  # type: ignore[attr-defined]


def test_cancelled_analysis_envelope_is_not_synthesized_as_success(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run(
        git_source,
        tmp_roots,
        monkeypatch,
        "cancelled_analysis_envelope",
        "cancelled-analysis",
        mode="analysis",
    )

    clone = _assert_retained_24h(outcome, tmp_roots)
    meta = WorkerMeta.read(meta_path(clone))
    assert "analysis backend returned stopReason=Cancelled" in (meta.error_message or "")
