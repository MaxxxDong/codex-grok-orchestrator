"""Create isolated standalone clones or independent non-Git copies."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path

from grok_worker.constants import CLONE_PREFIX, EXCLUDE_DIR_NAMES
from grok_worker.disclosure import (
    DisclosureError,
    DisclosureSummary,
    plan_disclosure,
    write_disclosure_summary,
)
from grok_worker.patch_capture import init_private_baseline
from grok_worker.task_id import TaskIdError, validate_task_id


class CloneError(RuntimeError):
    """Clone or copy creation failed."""


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def git_common_dir(repo: Path) -> Path:
    out = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True,
    ).strip()
    return Path(out).resolve()


def git_head(repo: Path) -> str:
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def git_is_dirty(repo: Path) -> bool:
    """True when non-ignored dirty/untracked material exists (standard excludes)."""
    out = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain", "-uall", "--untracked-files=all"],
        text=True,
    )
    # status --porcelain already respects ignore rules for untracked.
    return bool(out.strip())


def prove_independent_git(clone: Path) -> None:
    common = git_common_dir(clone)
    clone_res = clone.resolve()
    try:
        common.relative_to(clone_res)
    except ValueError as exc:
        raise CloneError(
            f"clone does not have its own git common dir: {common} not under {clone_res}"
        ) from exc


def make_task_id() -> str:
    return uuid.uuid4().hex[:12]


def clone_path_for(disposable_root: Path, task_id: str) -> Path:
    return disposable_root / f"{CLONE_PREFIX}{validate_task_id(task_id)}"


def create_git_clone(source: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise CloneError(f"clone destination already exists: {dest}")
    try:
        subprocess.run(
            ["git", "clone", "--no-hardlinks", str(source.resolve()), str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise CloneError(f"git clone failed: {exc.stderr or exc}") from exc
    prove_independent_git(dest)
    return git_head(dest)


def _ignore_copy(src: str, names: list[str]) -> set[str]:
    return {
        n
        for n in names
        if n in EXCLUDE_DIR_NAMES
        or n in {".grok-disposable", ".grok-artifacts"}
        or n.startswith(".venv")
    }


def create_nongit_copy(source: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise CloneError(f"copy destination already exists: {dest}")
    # Refuse outbound/absolute file *and directory* symlinks before materialization.
    for root, dirs, files in os.walk(source, followlinks=False):
        # Inspect directory symlinks (including escapes) before pruning them.
        for d in list(dirs):
            fp = Path(root) / d
            if not fp.is_symlink():
                continue
            try:
                target = os.readlink(fp)
            except OSError as exc:
                raise CloneError("cannot read directory symlink (path redacted)") from exc
            if target.startswith("/") or (len(target) > 1 and target[1] == ":"):
                raise CloneError(
                    "refusing non-git copy: absolute directory symlink escape (path redacted)"
                )
            try:
                resolved = (fp.parent / target).resolve()
                resolved.relative_to(source.resolve())
            except (OSError, ValueError) as exc:
                raise CloneError(
                    "refusing non-git copy: directory symlink escapes source tree "
                    "(path redacted)"
                ) from exc
        dirs[:] = [
            d
            for d in dirs
            if d not in EXCLUDE_DIR_NAMES and not (Path(root) / d).is_symlink()
        ]
        for name in files:
            fp = Path(root) / name
            if not fp.is_symlink():
                continue
            try:
                target = os.readlink(fp)
            except OSError as exc:
                raise CloneError("cannot read symlink (path redacted)") from exc
            if target.startswith("/") or (len(target) > 1 and target[1] == ":"):
                raise CloneError(
                    "refusing non-git copy: absolute symlink escape (path redacted)"
                )
            try:
                resolved = (fp.parent / target).resolve()
                resolved.relative_to(source.resolve())
            except (OSError, ValueError) as exc:
                raise CloneError(
                    "refusing non-git copy: symlink escapes source tree (path redacted)"
                ) from exc
    shutil.copytree(
        source.resolve(),
        dest,
        symlinks=True,
        ignore=_ignore_copy,
        ignore_dangling_symlinks=True,
    )
    return init_private_baseline(dest)


def _excluded_rel(rel: str) -> bool:
    return any(p in EXCLUDE_DIR_NAMES for p in Path(rel).parts) or Path(rel).name.startswith(
        "prompt-"
    )


def _copy_path(src_f: Path, dst_f: Path) -> None:
    dst_f.parent.mkdir(parents=True, exist_ok=True)
    if src_f.is_symlink():
        if dst_f.exists() or dst_f.is_symlink():
            dst_f.unlink()
        os.symlink(os.readlink(src_f), dst_f)
    elif src_f.is_file():
        shutil.copy2(src_f, dst_f, follow_symlinks=False)


def _untracked_rels(repo: Path) -> list[str]:
    """Untracked non-ignored paths only (standard Git excludes)."""
    raw = subprocess.check_output(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"]
    )
    out: list[str] = []
    for b in raw.split(b"\0"):
        if not b:
            continue
        rel = b.decode("utf-8", errors="surrogateescape")
        if not _excluded_rel(rel):
            out.append(rel)
    return out


def _apply_dirty_to_clone(
    source: Path,
    clone: Path,
    *,
    allowlist: Sequence[str] | None = None,
) -> str:
    """Apply tracked staged+unstaged via binary HEAD diff; copy untracked; commit.

    When *allowlist* is set, only those repository-relative paths are materialised
    from the dirty inventory. Ignored paths are never copied.
    """
    allowed = set(allowlist) if allowlist is not None else None

    if allowed is None:
        diff = subprocess.check_output(["git", "-C", str(source), "diff", "--binary", "HEAD"])
        if diff.strip():
            proc = subprocess.run(
                ["git", "-C", str(clone), "apply", "--binary", "--whitespace=nowarn"],
                input=diff,
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
                raise CloneError(f"failed applying dirty diff to clone: {err}")
        for rel in _untracked_rels(source):
            src_f = source / rel
            if src_f.is_symlink() or src_f.is_file():
                _copy_path(src_f, clone / rel)
    else:
        # Path-scoped: apply full HEAD diff then reset unlisted paths, or apply
        # path-limited diffs. Prefer path-limited for determinism.
        if allowed:
            # Tracked changes for allowlisted paths.
            tracked = subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(source),
                    "diff",
                    "--binary",
                    "HEAD",
                    "--",
                    *sorted(allowed),
                ]
            )
            if tracked.strip():
                proc = subprocess.run(
                    ["git", "-C", str(clone), "apply", "--binary", "--whitespace=nowarn"],
                    input=tracked,
                    capture_output=True,
                    check=False,
                )
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
                    raise CloneError(f"failed applying dirty diff to clone: {err}")
            untracked = set(_untracked_rels(source))
            for rel in sorted(allowed):
                if rel in untracked:
                    src_f = source / rel
                    if src_f.is_symlink() or src_f.is_file():
                        _copy_path(src_f, clone / rel)
                elif not (source / rel).exists() and not (source / rel).is_symlink():
                    # Deleted tracked path: ensure absence in clone after diff apply.
                    dst = clone / rel
                    if dst.exists() or dst.is_symlink():
                        if dst.is_dir() and not dst.is_symlink():
                            shutil.rmtree(dst)
                        else:
                            dst.unlink(missing_ok=True)

    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.name=grok-worker",
            "-c",
            "user.email=grok-worker@localhost",
            "commit",
            "--allow-empty",
            "-m",
            "lifecycle dirty source baseline",
        ],
        check=True,
        capture_output=True,
    )
    return git_head(clone)


def source_state_fingerprint(
    source: Path,
    *,
    allowlist: Sequence[str] | None = None,
) -> str:
    """Hash HEAD + binary diff + relevant untracked contents/symlink targets.

    Untracked discovery always uses standard excludes (never ignored files).
    When *allowlist* is set, only those paths contribute beyond HEAD.
    """
    h = hashlib.sha256()
    if is_git_repo(source):
        h.update(git_head(source).encode())
        if allowlist is not None:
            paths = sorted(set(allowlist))
            if paths:
                h.update(
                    subprocess.check_output(
                        ["git", "-C", str(source), "diff", "--binary", "HEAD", "--", *paths]
                    )
                )
            untracked = set(_untracked_rels(source))
            for rel in paths:
                if rel not in untracked:
                    continue
                h.update(rel.encode())
                fp = source / rel
                if fp.is_symlink():
                    h.update(b"link:")
                    h.update(os.readlink(fp).encode("utf-8", errors="surrogateescape"))
                elif fp.is_file():
                    try:
                        h.update(fp.read_bytes())
                    except OSError:
                        pass
        else:
            h.update(
                subprocess.check_output(["git", "-C", str(source), "diff", "--binary", "HEAD"])
            )
            for rel in _untracked_rels(source):
                h.update(rel.encode())
                fp = source / rel
                if fp.is_symlink():
                    h.update(b"link:")
                    h.update(os.readlink(fp).encode("utf-8", errors="surrogateescape"))
                elif fp.is_file():
                    try:
                        h.update(fp.read_bytes())
                    except OSError:
                        pass
    else:
        for root, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = [d for d in sorted(dirs) if d not in EXCLUDE_DIR_NAMES]
            for f in sorted(files):
                fp = Path(root) / f
                if fp.is_symlink():
                    continue
                h.update(str(fp.relative_to(source)).encode())
                try:
                    h.update(fp.read_bytes())
                except OSError:
                    pass
    return h.hexdigest()[:16]


def create_prompt_only_workspace(
    disposable_root: Path,
    task_id: str,
) -> tuple[Path, str, None, DisclosureSummary]:
    """Fresh empty managed workspace for prompt-only research (no source tree).

    Uses a private git baseline so the three-file artifact contract still works.
    ``source_realpath`` remains the honest ``prompt-only`` sentinel in lifecycle.
    """
    try:
        validate_task_id(task_id)
    except TaskIdError as exc:
        raise CloneError(str(exc)) from exc
    dest = clone_path_for(disposable_root, task_id)
    if dest.exists() or dest.is_symlink():
        raise CloneError(f"refusing existing path: {dest}")
    dest.mkdir(parents=True, exist_ok=False)
    base = init_private_baseline(dest)
    summary = DisclosureSummary(
        source_kind="prompt-only",
        base_sha=base,
        risk_decision="allow",
        reason_codes=["prompt_only"],
    )
    write_disclosure_summary(dest, summary)
    return dest, base, None, summary


def create_workspace(
    source: Path,
    disposable_root: Path,
    task_id: str,
    *,
    include_dirty: bool = False,
    dirty_allowlist: Sequence[str] | None = None,
) -> tuple[Path, str, str | None, DisclosureSummary]:
    """Returns (clone_path, base_commit, source_state_fingerprint_or_none, disclosure)."""
    try:
        validate_task_id(task_id)
    except TaskIdError as exc:
        raise CloneError(str(exc)) from exc
    dest = clone_path_for(disposable_root, task_id)
    if dest.exists() or dest.is_symlink():
        raise CloneError(f"refusing existing path: {dest}")

    try:
        summary = plan_disclosure(
            source,
            include_dirty=include_dirty,
            dirty_allowlist=dirty_allowlist,
            prompt_only=False,
            is_git=is_git_repo(source),
        )
    except DisclosureError as exc:
        raise CloneError(str(exc)) from exc

    if is_git_repo(source):
        dirty = git_is_dirty(source)
        allow = list(summary.included_dirty_paths) if (include_dirty or dirty_allowlist) else None
        if dirty and not include_dirty and not dirty_allowlist:
            raise CloneError(
                "source git checkout is dirty; refuse to launch Grok. "
                "Pass repeatable --include-dirty-path PATH (explicit allowlist). "
                "Bare --include-dirty is not accepted when nonignored dirty material "
                "exists. Ignored paths are never copied."
            )
        create_git_clone(source, dest)
        if dirty and (include_dirty or dirty_allowlist):
            # Allowlist-only materialization; bare include_dirty with only ignored
            # dirt yields a clean HEAD clone (plan_disclosure already refused
            # nonignored bare --include-dirty).
            if allow:
                base = _apply_dirty_to_clone(source, dest, allowlist=allow)
            else:
                base = git_head(dest)
            fp = source_state_fingerprint(source, allowlist=allow)
            summary.base_sha = base
            write_disclosure_summary(dest, summary)
            return dest, base, fp, summary
        summary.base_sha = git_head(dest)
        write_disclosure_summary(dest, summary)
        return dest, git_head(dest), None, summary

    base = create_nongit_copy(source, dest)
    summary.base_sha = base
    write_disclosure_summary(dest, summary)
    return dest, base, source_state_fingerprint(source), summary
