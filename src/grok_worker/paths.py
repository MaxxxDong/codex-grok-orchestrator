"""Path resolution helpers for disposable roots, artifacts, and shared cache."""

from __future__ import annotations

from pathlib import Path

from grok_worker.constants import META_DIR_NAME, META_FILE_NAME


def default_disposable_root(source: Path) -> Path:
    """Default: <source-parent-or-root>/.grok-disposable."""
    src = source.resolve()
    if (src / ".git").exists() or (src / ".git").is_file():
        parent = src.parent
    else:
        parent = src if src.is_dir() else src.parent
    return parent / ".grok-disposable"


def default_artifact_root(disposable_root: Path) -> Path:
    return disposable_root.resolve().parent / ".grok-artifacts"


def default_shared_cache_root() -> Path:
    from grok_worker.cache_policy import default_cache_root

    return default_cache_root()


def meta_dir(clone: Path) -> Path:
    return clone / META_DIR_NAME


def meta_path(clone: Path) -> Path:
    return meta_dir(clone) / META_FILE_NAME


def is_managed_clone(clone: Path) -> bool:
    p = meta_path(clone)
    return p.is_file() and not clone.is_symlink()


def artifact_outside_clone(artifact: Path, clone: Path, disposable_root: Path) -> bool:
    """True when artifact path is outside the clone and disposable root."""
    try:
        art = artifact.resolve()
        cl = clone.resolve()
        root = disposable_root.resolve()
    except OSError:
        return False
    if art == cl or art == root:
        return False
    try:
        art.relative_to(cl)
        return False  # under clone
    except ValueError:
        pass
    try:
        art.relative_to(root)
        return False  # under disposable root
    except ValueError:
        pass
    return True
