"""Explicit classification/import of unmarked legacy disposable clones."""

from __future__ import annotations

import subprocess
from datetime import timedelta
from pathlib import Path

from grok_worker.artifacts import (
    ArtifactError,
    collect_artifacts,
    verify_artifact_contract,
)
from grok_worker.clone import is_git_repo
from grok_worker.constants import MANAGED_BY, SCHEMA_VERSION
from grok_worker.locks import root_lock
from grok_worker.models import LegacyClass, WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import default_artifact_root, meta_path
from grok_worker.safety import is_direct_child, resolve_no_symlink_leaf

__all__ = ["LegacyClass", "LegacyError", "import_legacy", "list_unmarked"]


class LegacyError(RuntimeError):
    """Legacy import/classification failed."""


def _git_ok(clone: Path, args: list[str]) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(clone), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def _validate_commit(clone: Path, commit: str) -> str:
    out = _git_ok(clone, ["rev-parse", "--verify", f"{commit}^{{commit}}"])
    if not out:
        raise LegacyError(f"invalid or missing base commit: {commit}")
    return out


def _infer_git_base(clone: Path) -> str:
    """Infer base from upstream first, then origin/HEAD. Never default to HEAD."""
    for args in (
        ["rev-parse", "--verify", "@{upstream}"],
        ["rev-parse", "--verify", "refs/remotes/origin/HEAD"],
    ):
        cand = _git_ok(clone, args)
        if cand:
            return _validate_commit(clone, cand)
    raise LegacyError(
        "refusing Git legacy destructive archive: no current-branch upstream "
        "and no refs/remotes/origin/HEAD; pass explicit --base-commit "
        "(never defaults to HEAD, which would drop local commits)"
    )


def _resolve_legacy_base(clone: Path, base_commit: str | None) -> str:
    if base_commit:
        if not is_git_repo(clone):
            raise LegacyError("non-Git legacy has no commit object space; cannot use --base-commit")
        return _validate_commit(clone, base_commit)
    if is_git_repo(clone):
        return _infer_git_base(clone)
    raise LegacyError(
        "non-Git legacy destructive classification requires a reviewed baseline "
        "mechanism; refusing to mutate the directory or create an empty archive. "
        "Use classification=keep, or convert to a Git repo with an explicit base."
    )


def _archive_legacy(
    clone: Path,
    meta: WorkerMeta,
    artifact_root: Path,
    *,
    disposable_root: Path,
    base_commit: str | None,
) -> Path:
    """Capture external legacy artifact bundle before destructive classification."""
    resolved = _resolve_legacy_base(clone, base_commit)
    meta.base_commit = resolved
    try:
        art = collect_artifacts(
            clone,
            meta,
            artifact_root,
            disposable_root=disposable_root,
            audit={
                "session": {"name": None, "closed": True, "mode": "legacy-import"},
                "cleanup_receipt": {
                    "cloneDeletionAuthorized": True,
                    "sessionClosed": True,
                    "requestedState": str(meta.state),
                },
            },
        )
        verify_artifact_contract(art)
    except (ArtifactError, OSError) as exc:
        raise LegacyError(f"legacy archive failed (refusing classification): {exc}") from exc
    meta.artifact_path = str(art)
    meta.artifact_complete = True
    return art


def import_legacy(
    disposable_root: Path,
    name_or_path: str | Path,
    classification: LegacyClass,
    *,
    reason: str | None = None,
    source_realpath: str | None = None,
    artifact_root: Path | None = None,
    confirm_expire: bool = False,
    base_commit: str | None = None,
) -> WorkerMeta:
    """Mark a direct-child unmarked directory with lifecycle metadata.

    Ordinary GC never deletes unmarked legacy dirs.
    retain-24h and expire require a verified external artifact archive first.
    expire additionally requires nonempty --reason and --confirm-expire.
    keep is always allowed without archive.
    """
    root = disposable_root.resolve()
    candidate = Path(name_or_path)
    if not candidate.is_absolute():
        candidate = root / Path(name_or_path).name

    art_root = (artifact_root or default_artifact_root(root)).resolve()
    art_root.mkdir(parents=True, exist_ok=True)

    with root_lock(root):
        if candidate.is_symlink():
            raise LegacyError(f"refusing symlink: {candidate}")
        if not is_direct_child(candidate, root):
            raise LegacyError(f"not a direct child of disposable root: {candidate}")
        resolved = resolve_no_symlink_leaf(candidate)
        if not resolved.is_dir():
            raise LegacyError(f"not a directory: {resolved}")
        if meta_path(resolved).is_file():
            raise LegacyError(f"already managed: {resolved}")

        if classification == LegacyClass.EXPIRE:
            if not reason or not str(reason).strip():
                raise LegacyError("expire requires nonempty --reason")
            if not confirm_expire:
                raise LegacyError("expire requires --confirm-expire (destructive acknowledgement)")

        if classification == LegacyClass.KEEP:
            if not reason or not str(reason).strip():
                raise LegacyError("keep requires nonempty --reason")

        now = utc_now()
        task_id = resolved.name
        from grok_worker.task_id import TaskIdError, validate_task_id

        try:
            validate_task_id(task_id)
            safe_id = task_id
        except TaskIdError:
            import hashlib

            safe_id = "legacy-" + hashlib.sha256(task_id.encode()).hexdigest()[:12]

        meta = WorkerMeta(
            schema_version=SCHEMA_VERSION,
            task_id=safe_id,
            source_realpath=source_realpath or "unknown-legacy",
            clone_realpath=str(resolved),
            state=WorkerState.LEGACY_IMPORTED,
            created_at=dt_to_iso(now) or "",
            updated_at=dt_to_iso(now) or "",
            managed_by=MANAGED_BY,
            legacy_classification=str(classification),
        )

        if classification == LegacyClass.KEEP:
            meta.state = WorkerState.KEEP
            meta.keep_reason = reason.strip() if reason else "legacy-keep"
            meta.artifact_complete = False
            meta.write(meta_path(resolved))
            return meta

        try:
            _archive_legacy(
                resolved,
                meta,
                art_root,
                disposable_root=root,
                base_commit=base_commit,
            )
        except LegacyError:
            raise

        if classification == LegacyClass.RETAIN_24H:
            meta.state = WorkerState.FAILED
            meta.retention_deadline = dt_to_iso(now + timedelta(hours=24))
            meta.legacy_classification = str(classification)
            meta.error_message = reason or "legacy retain-24h"
        elif classification == LegacyClass.EXPIRE:
            meta.state = WorkerState.FAILED
            meta.retention_deadline = dt_to_iso(now - timedelta(seconds=1))
            meta.legacy_classification = str(classification)
            meta.error_message = reason
            meta.keep_reason = None
        else:
            raise LegacyError(f"unknown classification: {classification}")

        meta.write(meta_path(resolved))
        return meta


def list_unmarked(disposable_root: Path) -> list[str]:
    root = disposable_root
    if not root.is_dir():
        return []
    out: list[str] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
            continue
        if not meta_path(child).is_file():
            out.append(child.name)
    return out
