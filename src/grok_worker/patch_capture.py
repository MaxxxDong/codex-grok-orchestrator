"""Binary-safe patch capture via temporary Git index (no source mutation)."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from grok_worker.constants import EXCLUDE_DIR_NAMES, EXCLUDE_FILE_PREFIXES


class PatchError(RuntimeError):
    """Patch collection failed."""


def _should_exclude(rel: str) -> bool:
    parts = Path(rel).parts
    if not parts:
        return True
    for p in parts:
        if p in EXCLUDE_DIR_NAMES:
            return True
        if p.startswith(".venv"):
            return True
    name = parts[-1]
    for prefix in EXCLUDE_FILE_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def _run_git(clone: Path, args: list[str], *, env: dict[str, str] | None = None) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(clone), *args],
        capture_output=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
        raise PatchError(f"git {' '.join(args)} failed ({proc.returncode}): {err}")
    return proc.stdout


def _git_ok_diff(clone: Path, args: list[str], *, env: dict[str, str] | None = None) -> bytes:
    """Run git diff-like command; 0 and 1 are both success (1 = differences)."""
    proc = subprocess.run(
        ["git", "-C", str(clone), *args],
        capture_output=True,
        check=False,
        env=env,
    )
    if proc.returncode not in (0, 1):
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
        raise PatchError(f"git {' '.join(args)} failed ({proc.returncode}): {err}")
    return proc.stdout


def collect_git_patch(clone: Path, base_commit: str | None, out_file: Path) -> None:
    """Capture all worker changes vs base using a temporary index.

    Includes committed, staged, worktree, untracked text/binary/empty, unusual
    names. Excludes operational outputs and caches. Does not mutate the
    source checkout's real index.
    """
    if not base_commit:
        raise PatchError("base_commit is required for git patch capture")

    # Prove base is reachable
    _run_git(clone, ["cat-file", "-e", f"{base_commit}^{{commit}}"])

    with tempfile.NamedTemporaryFile(prefix="gw-index-", delete=False) as tmp:
        index_path = tmp.name
    try:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = index_path
        # Start index from baseline commit tree
        _run_git(clone, ["read-tree", base_commit], env=env)

        # Stage all worktree paths into the temp index, then unstage exclusions
        # `git add -A` stages tracked mods + untracked + removals into temp index
        add_proc = subprocess.run(
            ["git", "-C", str(clone), "add", "-A", "--", "."],
            capture_output=True,
            check=False,
            env=env,
        )
        if add_proc.returncode != 0:
            err = (add_proc.stderr or b"").decode("utf-8", errors="replace")
            raise PatchError(f"git add -A failed: {err}")

        # Remove excluded paths from the temp index
        ls = _run_git(clone, ["ls-files", "-z"], env=env)
        for raw in ls.split(b"\0"):
            if not raw:
                continue
            rel = raw.decode("utf-8", errors="surrogateescape")
            if _should_exclude(rel):
                rm = subprocess.run(
                    ["git", "-C", str(clone), "rm", "-f", "--cached", "--", rel],
                    capture_output=True,
                    check=False,
                    env=env,
                )
                if rm.returncode not in (0, 1):
                    err = (rm.stderr or b"").decode("utf-8", errors="replace")
                    raise PatchError(f"git rm --cached failed for {rel}: {err}")

        # Also ensure excluded tracked paths from base stay as in base by
        # restoring them from base if we need exact base for those — already
        # removed from index so they appear as deletions vs base; re-add from base
        base_ls = _run_git(clone, ["ls-tree", "-r", "--name-only", "-z", base_commit])
        for raw in base_ls.split(b"\0"):
            if not raw:
                continue
            rel = raw.decode("utf-8", errors="surrogateescape")
            if _should_exclude(rel):
                # Restore base blob into temp index so excluded paths don't appear in patch
                blob = subprocess.run(
                    ["git", "-C", str(clone), "ls-tree", base_commit, "--", rel],
                    capture_output=True,
                    check=False,
                )
                if blob.returncode == 0 and blob.stdout.strip():
                    # format: mode type hash\tname
                    line = blob.stdout.decode("utf-8", errors="replace").strip()
                    parts = line.split()
                    if len(parts) >= 3:
                        mode, _typ, sha = parts[0], parts[1], parts[2].split("\t")[0]
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                str(clone),
                                "update-index",
                                "--add",
                                "--cacheinfo",
                                f"{mode},{sha},{rel}",
                            ],
                            capture_output=True,
                            check=False,
                            env=env,
                        )

        diff = _git_ok_diff(
            clone, ["diff", "--cached", "--binary", "--full-index", base_commit], env=env
        )
        out_file.write_bytes(diff)
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass


def init_private_baseline(clone: Path) -> str:
    """Initialize a private git repo in a non-git copy and commit current tree.

    Preserves symlinks as symlinks (caller must have copied with symlinks=True).
    Returns the baseline commit hash.
    """
    if not (clone / ".git").exists():
        subprocess.run(
            ["git", "-C", str(clone), "init"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(clone), "config", "user.email", "lifecycle@local"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(clone), "config", "user.name", "lifecycle"],
            check=True,
            capture_output=True,
        )
    # Stage everything except operational dirs
    env = os.environ.copy()
    subprocess.run(
        ["git", "-C", str(clone), "add", "-A", "--", "."],
        check=True,
        capture_output=True,
        env=env,
    )
    # Unstage exclusions
    ls_raw = subprocess.check_output(["git", "-C", str(clone), "ls-files", "-z"])
    ls_bytes = ls_raw if isinstance(ls_raw, (bytes, bytearray)) else str(ls_raw).encode()
    for raw in ls_bytes.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="surrogateescape")
        if _should_exclude(rel):
            subprocess.run(
                ["git", "-C", str(clone), "rm", "-f", "--cached", "--", rel],
                capture_output=True,
                check=False,
            )
    subprocess.run(
        ["git", "-C", str(clone), "commit", "--allow-empty", "-m", "lifecycle baseline"],
        check=True,
        capture_output=True,
    )
    head = subprocess.check_output(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], text=True
    ).strip()
    return head
