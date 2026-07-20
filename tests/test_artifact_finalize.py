"""Artifact finalization gates: exceptions, tamper, never success-delete."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from grok_worker.artifacts import ArtifactError, verify_manifest
from grok_worker.constants import MANAGED_BY, SCHEMA_VERSION
from grok_worker.gc import gc_disposable_root, should_delete
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import meta_dir, meta_path
from grok_worker.runner import RunConfig, run_worker


def test_artifact_failure_preserves_primary_backend_error() -> None:
    from grok_worker.finalize import _compose_artifact_error_message

    message = _compose_artifact_error_message(
        "upstream native failure: response truncated by max_tokens",
        ArtifactError("disk full"),
    )

    assert message.startswith("upstream native failure")
    assert "secondary artifact finalization failed: disk full" in message


def test_artifact_exception_never_deletes(
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
        task_id="artfail",
        skip_post_gc=True,
    )
    with mock.patch(
        "grok_worker.finalize.collect_artifacts",
        side_effect=ArtifactError("disk full"),
    ):
        outcome = run_worker(cfg)
    assert outcome.state == "failed"
    assert outcome.exit_code != 0
    assert outcome.clone_path is not None
    clone = Path(outcome.clone_path)
    assert clone.is_dir()
    meta = WorkerMeta.read(meta_path(clone))
    assert meta.state == WorkerState.FAILED
    assert meta.retention_deadline is not None
    # post-GC must not erase
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert clone.name not in report.removed
    assert clone.is_dir()


def test_manifest_tamper_blocks_gc(tmp_roots: dict[str, Path]) -> None:
    clone = tmp_roots["disposable"] / "tamper1"
    clone.mkdir()
    art = tmp_roots["artifacts"] / "tamper1"
    art.mkdir()
    (art / "changes.patch").write_text("p", encoding="utf-8")
    (art / "exit-status.json").write_text("{}", encoding="utf-8")
    (art / "lifecycle.json").write_text("{}", encoding="utf-8")
    from grok_worker.artifacts import write_manifest

    write_manifest(art)
    # tamper
    (art / "changes.patch").write_text("TAMPERED", encoding="utf-8")
    with __import__("pytest").raises(ArtifactError):
        verify_manifest(art)

    now = utc_now()
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="tamper1",
        source_realpath="/tmp/src",
        clone_realpath=str(clone.resolve()),
        state=WorkerState.SUCCESS,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        artifact_path=str(art.resolve()),
        artifact_complete=True,
    )
    meta_dir(clone).mkdir(parents=True, exist_ok=True)
    meta.write(meta_path(clone))
    assert not should_delete(
        meta, clone, datetime.now(UTC), disposable_root=tmp_roots["disposable"]
    )
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert "tamper1" not in report.removed
    assert clone.is_dir()


def test_success_without_artifact_complete_not_deleted(tmp_roots: dict[str, Path]) -> None:
    clone = tmp_roots["disposable"] / "halfok"
    clone.mkdir()
    (clone / "x").write_text("1", encoding="utf-8")
    now = utc_now()
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="halfok",
        source_realpath="/tmp/src",
        clone_realpath=str(clone.resolve()),
        state=WorkerState.SUCCESS,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        artifact_complete=False,
    )
    meta_dir(clone).mkdir(parents=True, exist_ok=True)
    meta.write(meta_path(clone))
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    # converted to failed or retained, never deleted
    assert "halfok" not in report.removed
    assert clone.is_dir()


def test_manifest_extra_file_rejected(tmp_roots: dict[str, Path]) -> None:
    art = tmp_roots["artifacts"] / "extra-art"
    art.mkdir()
    (art / "changes.patch").write_text("p", encoding="utf-8")
    (art / "exit-status.json").write_text("{}", encoding="utf-8")
    (art / "lifecycle.json").write_text("{}", encoding="utf-8")
    from grok_worker.artifacts import write_manifest

    write_manifest(art)
    (art / "evil-extra.txt").write_text("tamper", encoding="utf-8")
    with __import__("pytest").raises(ArtifactError, match="extra|not listed"):
        verify_manifest(art)


def test_staging_preexisting_preserved(tmp_roots: dict[str, Path]) -> None:
    from grok_worker.artifacts import collect_artifacts
    from grok_worker.constants import STAGING_PREFIX
    from tests.conftest import init_git_repo

    clone = tmp_roots["disposable"] / "stgclone"
    base = init_git_repo(clone)
    staging = tmp_roots["artifacts"] / f"{STAGING_PREFIX}stg1"
    staging.mkdir()
    sentinel = staging / "preserve-me.txt"
    sentinel.write_text("keep-staging", encoding="utf-8")
    now = utc_now()
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="stg1",
        source_realpath="/tmp/src",
        clone_realpath=str(clone.resolve()),
        state=WorkerState.SUCCESS,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        base_commit=base,
    )
    with __import__("pytest").raises(ArtifactError, match="staging|preexisting|exists"):
        collect_artifacts(
            clone,
            meta,
            tmp_roots["artifacts"],
            disposable_root=tmp_roots["disposable"],
        )
    assert sentinel.is_file()
    assert sentinel.read_text(encoding="utf-8") == "keep-staging"


def test_unsafe_artifact_root_refused(tmp_roots: dict[str, Path]) -> None:
    from grok_worker.artifacts import collect_artifacts
    from tests.conftest import init_git_repo

    clone = tmp_roots["disposable"] / "unsafe-c"
    base = init_git_repo(clone)
    # Artifact root *inside* the clone must be refused
    inside = clone / "nested-arts"
    inside.mkdir()
    now = utc_now()
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="unsafe1",
        source_realpath="/tmp/src",
        clone_realpath=str(clone.resolve()),
        state=WorkerState.SUCCESS,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        base_commit=base,
    )
    with __import__("pytest").raises(ArtifactError, match="outside|unsafe|clone|disposable"):
        collect_artifacts(clone, meta, inside, disposable_root=tmp_roots["disposable"])
