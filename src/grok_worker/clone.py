"""Create isolated standalone clones or independent non-Git copies."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from grok_worker.constants import CLONE_PREFIX, EXCLUDE_DIR_NAMES
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
    out = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain", "-uall"], text=True
    )
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
    raw = subprocess.check_output(["git", "-C", str(repo), "ls-files", "--others", "-z"])
    out: list[str] = []
    for b in raw.split(b"\0"):
        if not b:
            continue
        rel = b.decode("utf-8", errors="surrogateescape")
        if not _excluded_rel(rel):
            out.append(rel)
    return out


def _apply_dirty_to_clone(source: Path, clone: Path) -> str:
    """Apply tracked staged+unstaged via binary HEAD diff; copy untracked; commit."""
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
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True, capture_output=True)
    # Command-scoped identity only: dirty-baseline commits must not depend on
    # host/global Git config (Ubuntu runners often have none) or mutate clone config.
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


def source_state_fingerprint(source: Path) -> str:
    """Hash HEAD + binary diff + relevant untracked contents/symlink targets."""
    h = hashlib.sha256()
    if is_git_repo(source):
        h.update(git_head(source).encode())
        h.update(subprocess.check_output(["git", "-C", str(source), "diff", "--binary", "HEAD"]))
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


def create_workspace(
    source: Path,
    disposable_root: Path,
    task_id: str,
    *,
    include_dirty: bool = False,
) -> tuple[Path, str, str | None]:
    """Returns (clone_path, base_commit, source_state_fingerprint_or_none)."""
    try:
        validate_task_id(task_id)
    except TaskIdError as exc:
        raise CloneError(str(exc)) from exc
    dest = clone_path_for(disposable_root, task_id)
    if dest.exists() or dest.is_symlink():
        raise CloneError(f"refusing existing path: {dest}")
    if is_git_repo(source):
        dirty = git_is_dirty(source)
        if dirty and not include_dirty:
            raise CloneError(
                "source git checkout is dirty; refuse to launch Grok. "
                "Pass --include-dirty to copy staged/unstaged/untracked state "
                "into the clone as the private baseline."
            )
        create_git_clone(source, dest)
        if dirty and include_dirty:
            return dest, _apply_dirty_to_clone(source, dest), source_state_fingerprint(source)
        return dest, git_head(dest), None
    return dest, create_nongit_copy(source, dest), source_state_fingerprint(source)
