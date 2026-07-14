"""Explicit keep with nonempty reason survives GC."""

from __future__ import annotations

from pathlib import Path

from grok_worker.gc import gc_disposable_root
from grok_worker.runner import RunConfig, run_worker


def test_explicit_keep_survives_gc(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
) -> None:
    cfg = RunConfig(
        source=git_source,
        prompt="keep me",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        task_id="keep01",
        keep_reason="audit trail",
    )
    outcome = run_worker(cfg)
    assert outcome.state == "keep"
    assert outcome.clone_path is not None
    clone = Path(outcome.clone_path)
    assert clone.is_dir()
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert clone.name not in report.removed
    assert clone.is_dir()
