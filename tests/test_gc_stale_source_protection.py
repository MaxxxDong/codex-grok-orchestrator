"""Regression tests for stale worker source paths in CLI garbage collection."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

from grok_worker.cli import main
from grok_worker.constants import MANAGED_BY, SCHEMA_VERSION
from grok_worker.legacy import LegacyClass, import_legacy
from grok_worker.locks import worker_lock
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import meta_dir, meta_path
from tests.conftest import init_git_repo


def _failed_worker(clone: Path, *, source: Path) -> None:
    clone.mkdir()
    now = utc_now()
    WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id=clone.name,
        source_realpath=str(source.resolve()),
        clone_realpath=str(clone.resolve()),
        state=WorkerState.FAILED,
        created_at=dt_to_iso(now) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        retention_deadline=dt_to_iso(now + timedelta(hours=20)),
        exit_code=-1,
        error_message="retained failure",
    ).write(meta_path(clone))


def _reclaimable_legacy(tmp_roots: dict[str, Path], name: str) -> Path:
    target = tmp_roots["disposable"] / name
    base = init_git_repo(target)
    (target / "untracked.txt").write_text("archive me\n", encoding="utf-8")
    meta = import_legacy(
        tmp_roots["disposable"],
        name,
        LegacyClass.EXPIRE,
        reason="test archive",
        artifact_root=tmp_roots["artifacts"],
        confirm_expire=True,
        base_commit=base,
    )
    assert meta.artifact_complete
    return target


def _gc(tmp_roots: dict[str, Path]) -> dict[str, object]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(
            [
                "gc",
                "--disposable-root",
                str(tmp_roots["disposable"]),
                "--shared-cache-root",
                str(tmp_roots["shared"]),
                "--artifact-root",
                str(tmp_roots["artifacts"]),
                "--json",
            ]
        )
    assert code == 0
    return json.loads(output.getvalue())


def test_inactive_failed_source_does_not_pin_reclaimable_clone(
    tmp_roots: dict[str, Path],
) -> None:
    target = _reclaimable_legacy(tmp_roots, "stale-source-target")
    dependent = tmp_roots["disposable"] / "inactive-dependent"
    _failed_worker(dependent, source=target)

    report = _gc(tmp_roots)

    assert "stale-source-target" in report["removed"]
    assert not target.exists()
    assert dependent.exists()


def test_active_worker_source_still_pins_reclaimable_clone(
    tmp_roots: dict[str, Path],
) -> None:
    target = _reclaimable_legacy(tmp_roots, "active-source-target")
    dependent = tmp_roots["disposable"] / "active-dependent"
    _failed_worker(dependent, source=target)

    lock = worker_lock(meta_dir(dependent))
    lock.acquire()
    try:
        report = _gc(tmp_roots)
    finally:
        lock.release()

    assert "active-source-target" not in report["removed"]
    assert target.exists()
