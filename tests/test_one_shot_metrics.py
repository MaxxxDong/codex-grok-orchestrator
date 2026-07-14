"""One-shot runs persist honest metrics and embed them in verification.txt."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from grok_worker.runner import RunConfig, run_worker
from tests.fake_acpx import write_fake_acpx


def _run(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    behavior: str,
    task_id: str,
) -> object:
    fake = write_fake_acpx(tmp_roots["root"] / "bin", behavior)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_ACPX_BEHAVIOR", behavior)
    return run_worker(
        RunConfig(
            source=git_source,
            prompt="x",
            disposable_root=tmp_roots["disposable"],
            artifact_root=tmp_roots["artifacts"],
            shared_cache_root=tmp_roots["shared"],
            acpx_bin=str(fake),
            prepare_deps=False,
            task_id=task_id,
            skip_post_gc=True,
        )
    )


def test_one_shot_success_appends_metrics_and_embeds_in_receipt(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run(git_source, tmp_roots, monkeypatch, "success", "os-met-ok")
    assert outcome.exit_code == 0  # type: ignore[attr-defined]
    assert outcome.state == "success"  # type: ignore[attr-defined]

    metrics_path = tmp_roots["shared"] / "metrics" / "worker-runs.jsonl"
    assert metrics_path.is_file()
    lines = [
        json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert len(lines) == 1
    record = lines[0]
    assert record["task_id"] == "os-met-ok"
    assert record["run_kind"] == "one-shot"
    assert record["acpx_exit_code"] == 0
    # Fake acpx does not emit token fields → honest unobservable metrics.
    assert record["observable"] is False
    assert record["input_tokens"] is None
    assert record["cached_tokens"] is None
    assert record["output_tokens"] is None
    assert record["cache_ratio"] is None

    art = Path(outcome.artifact_path or "")  # type: ignore[attr-defined]
    receipt = json.loads((art / "verification.txt").read_text(encoding="utf-8"))
    assert len(receipt["metrics"]) == 1
    assert receipt["metrics"][0]["task_id"] == "os-met-ok"
    assert receipt["metrics"][0]["observable"] is False
    # Metrics-only audit must not flip one-shot cleanup_receipt.sessionClosed.
    assert receipt["cleanup_receipt"]["sessionClosed"] is True
    worker = json.loads((art / "worker.log").read_text(encoding="utf-8"))
    assert worker["session"]["closed"] is True
    from grok_worker.artifacts import artifact_authorizes_clone_deletion

    assert artifact_authorizes_clone_deletion(art) is True


def test_one_shot_acp_failure_still_persists_metrics_and_stays_failed(
    git_source: Path,
    tmp_roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _run(git_source, tmp_roots, monkeypatch, "crash", "os-met-fail")
    assert outcome.exit_code != 0  # type: ignore[attr-defined]
    assert outcome.state == "failed"  # type: ignore[attr-defined]

    metrics_path = tmp_roots["shared"] / "metrics" / "worker-runs.jsonl"
    assert metrics_path.is_file()
    lines = [
        json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert len(lines) == 1
    record = lines[0]
    assert record["task_id"] == "os-met-fail"
    assert record["run_kind"] == "one-shot"
    assert record["acpx_exit_code"] == 99
    assert record["observable"] is False

    art = Path(outcome.artifact_path or "")  # type: ignore[attr-defined]
    assert art.is_dir()
    receipt = json.loads((art / "verification.txt").read_text(encoding="utf-8"))
    assert len(receipt["metrics"]) == 1
    assert receipt["metrics"][0]["acpx_exit_code"] == 99
    # Metrics must not flip a failure into success.
    worker = json.loads((art / "worker.log").read_text(encoding="utf-8"))
    assert worker["lifecycle"]["state"] == "failed"
    assert outcome.state == "failed"  # type: ignore[attr-defined]
