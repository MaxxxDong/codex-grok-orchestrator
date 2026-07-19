"""Successful run: exact external artifacts; clone deleted; hashes verified."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from grok_worker.artifacts import sha256_file, verify_artifact_contract
from grok_worker.runner import RunConfig, run_worker


def test_success_collects_artifacts_and_deletes_clone(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
) -> None:
    cfg = RunConfig(
        source=git_source,
        prompt="do the thing",
        backend="acp",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        task_id="succ01",
    )
    outcome = run_worker(cfg)
    assert outcome.exit_code == 0
    assert outcome.state == "success"
    assert outcome.clone_path is None
    clones = [
        p for p in tmp_roots["disposable"].iterdir() if p.is_dir() and not p.name.startswith(".")
    ]
    assert clones == []
    art = Path(outcome.artifact_path or "")
    assert art.is_dir()
    assert sorted(path.name for path in art.iterdir()) == [
        "changes.patch",
        "verification.txt",
        "worker.log",
    ]
    patch = (art / "changes.patch").read_text(encoding="utf-8", errors="replace")
    assert "feature.txt" in patch
    # Recompute every hash after success
    verify_artifact_contract(art)
    receipt = json.loads((art / "verification.txt").read_text(encoding="utf-8"))
    for name, expected in receipt["artifact_hashes"].items():
        assert sha256_file(art / name) == expected
    assert receipt["verification"][0]["content"] == "ok\n"


def test_success_manifest_hash_verification(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
) -> None:
    cfg = RunConfig(
        source=git_source,
        prompt="x",
        backend="acp",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        task_id="manihash",
    )
    outcome = run_worker(cfg)
    art = Path(outcome.artifact_path or "")
    verify_artifact_contract(art)


def test_post_gc_failure_does_not_override_success(
    git_source: Path,
    tmp_roots: dict[str, Path],
    path_with_fake_acpx: Path,
    capsys: object,
) -> None:
    cfg = RunConfig(
        source=git_source,
        prompt="x",
        backend="acp",
        disposable_root=tmp_roots["disposable"],
        artifact_root=tmp_roots["artifacts"],
        shared_cache_root=tmp_roots["shared"],
        acpx_bin=str(path_with_fake_acpx),
        prepare_deps=False,
        task_id="post-gc-warning",
    )
    with mock.patch(
        "grok_worker.worker_exec.gc_disposable_root", side_effect=OSError("gc lock denied")
    ):
        outcome = run_worker(cfg)

    assert outcome.exit_code == 0
    assert outcome.state == "success"
    assert "post-run GC skipped" in capsys.readouterr().err  # type: ignore[attr-defined]
