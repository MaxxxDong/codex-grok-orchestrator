"""Unknown schema / clone mismatch preservation; managed_by checks."""

from __future__ import annotations

from pathlib import Path

from grok_worker.constants import MANAGED_BY, SCHEMA_VERSION
from grok_worker.gc import gc_disposable_root
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import meta_dir, meta_path


def test_unknown_schema_preserved(tmp_roots: dict[str, Path]) -> None:
    clone = tmp_roots["disposable"] / "unkschema"
    clone.mkdir()
    (clone / "x").write_text("1", encoding="utf-8")
    now = utc_now()
    meta = WorkerMeta(
        schema_version=999,
        task_id="unkschema",
        source_realpath="/tmp/s",
        clone_realpath=str(clone.resolve()),
        state=WorkerState.SUCCESS,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        artifact_complete=True,
    )
    meta_dir(clone).mkdir(parents=True, exist_ok=True)
    meta.write(meta_path(clone))
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert "unkschema" not in report.removed
    assert clone.is_dir()
    assert any("unkschema" in x for x in report.skipped_untrusted)


def test_clone_mismatch_preserved(tmp_roots: dict[str, Path]) -> None:
    clone = tmp_roots["disposable"] / "mismatch"
    clone.mkdir()
    (clone / "x").write_text("1", encoding="utf-8")
    now = utc_now()
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="mismatch",
        source_realpath="/tmp/s",
        clone_realpath="/tmp/other-path-not-this",
        state=WorkerState.SUCCESS,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        artifact_complete=True,
    )
    meta_dir(clone).mkdir(parents=True, exist_ok=True)
    meta.write(meta_path(clone))
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert "mismatch" not in report.removed
    assert clone.is_dir()


def test_missing_managed_by_preserved(tmp_roots: dict[str, Path]) -> None:
    clone = tmp_roots["disposable"] / "nomanage"
    clone.mkdir()
    (clone / "x").write_text("1", encoding="utf-8")
    now = utc_now()
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="nomanage",
        source_realpath="/tmp/s",
        clone_realpath=str(clone.resolve()),
        state=WorkerState.FAILED,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by="",
        retention_deadline="2000-01-01T00:00:00+00:00",
    )
    meta_dir(clone).mkdir(parents=True, exist_ok=True)
    meta.write(meta_path(clone))
    report = gc_disposable_root(tmp_roots["disposable"], clean_tmp=False)
    assert "nomanage" not in report.removed
    assert clone.is_dir()
