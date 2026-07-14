"""Deletion and path safety: never remove protected or non-managed paths."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


class SafetyError(RuntimeError):
    """Raised when a path is not safe to mutate or delete."""


def resolve_no_symlink_leaf(path: Path) -> Path:
    """Resolve parents but require the final path component itself is not a symlink."""
    path = Path(path)
    if path.exists() and path.is_symlink():
        raise SafetyError(f"refusing symlink as managed path: {path}")
    parent = path.parent if path.parent != path else path
    resolved_parent = parent.resolve()
    return resolved_parent / path.name


def is_direct_child(child: Path, root: Path) -> bool:
    try:
        child_res = resolve_no_symlink_leaf(child)
        root_res = root.resolve()
    except (OSError, SafetyError):
        return False
    try:
        child_res.relative_to(root_res)
    except ValueError:
        return False
    return child_res.parent == root_res


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return a.st_ino == b.st_ino and a.st_dev == b.st_dev


def assert_safe_delete_target(
    target: Path,
    *,
    disposable_root: Path,
    protected: list[Path],
) -> tuple[Path, os.stat_result]:
    """Return (verified path, lstat) for a deletable direct-child target."""
    if target.is_symlink():
        raise SafetyError(f"refusing to delete symlink: {target}")
    root = disposable_root.resolve()
    if not target.exists():
        raise SafetyError(f"delete target does not exist: {target}")
    if not is_direct_child(target, root):
        raise SafetyError(f"not a direct child of disposable root: {target}")
    resolved = resolve_no_symlink_leaf(target)
    try:
        st = resolved.lstat()
    except OSError as exc:
        raise SafetyError(f"cannot lstat delete target: {resolved}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise SafetyError(f"refusing to delete symlink: {resolved}")
    if resolved == root:
        raise SafetyError("refusing to delete disposable root")
    for p in protected:
        try:
            pref = p.resolve()
        except OSError:
            continue
        if pref == root:
            continue
        if resolved == pref:
            raise SafetyError(f"refusing to delete protected path: {resolved}")
        try:
            pref.relative_to(resolved)
            if pref != resolved:
                raise SafetyError(f"refusing to delete ancestor of protected path: {resolved}")
        except ValueError:
            pass
    for dangerous in (Path.home(), Path("/")):
        try:
            if resolved == dangerous.resolve():
                raise SafetyError(f"refusing to delete dangerous path: {resolved}")
        except OSError:
            continue
    return resolved, st


def safe_unlink(path: Path, *, disposable_root: Path, protected: list[Path]) -> None:
    """Guarded single-file deletion (direct child of disposable_root)."""
    verified, st = assert_safe_delete_target(
        path, disposable_root=disposable_root, protected=protected
    )
    if not stat.S_ISREG(st.st_mode) and not stat.S_ISFIFO(st.st_mode):
        # allow regular files and sockets for tmp cleanup
        if not stat.S_ISSOCK(st.st_mode):
            raise SafetyError(f"safe_unlink only for files: {verified}")
    # recheck not a symlink at unlink time
    if verified.is_symlink():
        raise SafetyError(f"TOCTOU: became symlink: {verified}")
    verified.unlink(missing_ok=False)


def safe_rmtree(target: Path, *, disposable_root: Path, protected: list[Path]) -> None:
    """Symlink-attack-resistant recursive delete of a direct-child directory."""
    verified, expected_st = assert_safe_delete_target(
        target, disposable_root=disposable_root, protected=protected
    )
    # Re-check identity immediately before deletion
    try:
        current_st = verified.lstat()
    except OSError as exc:
        raise SafetyError(f"delete target vanished: {verified}: {exc}") from exc
    if not _same_file(expected_st, current_st):
        raise SafetyError(f"TOCTOU: delete target identity changed: {verified}")
    if stat.S_ISLNK(current_st.st_mode):
        raise SafetyError(f"TOCTOU: became symlink: {verified}")
    if not stat.S_ISDIR(current_st.st_mode):
        if stat.S_ISREG(current_st.st_mode):
            verified.unlink()
            return
        raise SafetyError(f"not a directory or file: {verified}")

    if getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        # Python with fd-based rmtree (dir_fd / openat)
        def _onexc(func: object, path: str, exc: BaseException) -> None:  # noqa: ARG001
            raise SafetyError(f"rmtree failed at {path}: {exc}") from exc

        try:
            shutil.rmtree(verified, onexc=_onexc)
        except TypeError:
            # older signature
            def _onerror(
                func: object,
                path: str,
                exc_info: object,  # noqa: ARG001
            ) -> None:
                raise SafetyError(f"rmtree failed at {path}")

            shutil.rmtree(verified, onerror=_onerror)
        return

    # Fail closed when fd-based protection is unavailable
    raise SafetyError(
        "safe_rmtree requires shutil.rmtree.avoids_symlink_attacks; refusing unsafe deletion"
    )


def dir_size_bytes(path: Path) -> int:
    """Compute size without following directory symlinks."""
    total = 0
    if not path.exists() or path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    for root, dirs, files in os.walk(path, followlinks=False):
        keep: list[str] = []
        for d in dirs:
            dp = Path(root) / d
            if not dp.is_symlink():
                keep.append(d)
        dirs[:] = keep
        for f in files:
            fp = Path(root) / f
            try:
                if fp.is_symlink():
                    continue
                total += fp.stat().st_size
            except OSError:
                continue
    return total
