"""Locked Python and npm dependency preparation for disposable clones."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from shutil import which

from grok_worker.locks import fingerprint_lock
from grok_worker.models import atomic_replace
from grok_worker.process_launch import hidden_startup_info

SYNC_CONTRACT: tuple[str, ...] = (
    "--frozen",
    "--all-groups",
    "--all-extras",
    "--no-install-project",
)
NPM_SYNC_CONTRACT: tuple[str, ...] = (
    "ci",
    "--prefer-offline",
    "--no-audit",
    "--no-fund",
    "--ignore-scripts",
)
# ponytail: keep lifecycle scripts disabled; add an explicit trusted opt-in only
# when a locked project proves that it requires a postinstall step.
READY_MARKER_NAME = ".grok-worker-ready"
NPM_READY_MARKER_NAME = ".grok-worker-ready"


class DepsError(RuntimeError):
    """Shared dependency preparation failed (hard failure)."""


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def interpreter_identity(executable: str | None = None) -> str:
    """Stable interpreter identity across ephemeral launcher symlink paths.

    ``uv run --no-project --with ...`` exposes a fresh
    ``.../builds-v0/.tmpXXXX/bin/python`` as ``sys.executable`` each launch, while
    ``os.path.realpath`` of that path points at the same managed interpreter.
    Hash the resolved path so shared-env fingerprints stay stable across launches.
    """
    raw = executable if executable is not None else sys.executable
    try:
        return os.path.realpath(raw)
    except OSError:
        return raw


def compute_fingerprint(source: Path) -> str:
    """Fingerprint: dep inputs + Python identity + platform + exact sync options."""
    h = hashlib.sha256()
    h.update(platform.system().encode())
    h.update(platform.machine().encode())
    h.update(sys.version.encode())
    h.update(interpreter_identity().encode())
    for name in ("uv.lock", "pyproject.toml", "requirements.txt", "requirements.lock"):
        p = source / name
        if p.is_file() and not p.is_symlink():
            h.update(name.encode())
            h.update(_hash_file(p).encode())
    h.update(" ".join(SYNC_CONTRACT).encode())
    return h.hexdigest()[:24]


def shared_paths(shared_root: Path, fingerprint: str) -> tuple[Path, Path, Path]:
    root = shared_root.resolve()
    return root / "uv", root / "venvs" / fingerprint, root / "locks"


def build_uv_sync_cmd(source: Path, *, has_lock: bool) -> list[str]:
    if not has_lock:
        raise DepsError("uv.lock required for frozen sync")
    return [
        "uv",
        "sync",
        *SYNC_CONTRACT,
        "--directory",
        str(source.resolve()),
    ]


def build_npm_sync_cmd(npm_executable: str) -> list[str]:
    resolved = Path(npm_executable)
    if os.name == "nt" and resolved.suffix.lower() in {".cmd", ".bat"}:
        entry = resolved.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        node = which("node")
        if not node or not entry.is_file():
            raise DepsError("cannot resolve npm-cli.js behind the Windows npm launcher")
        return [node, str(entry), *NPM_SYNC_CONTRACT]
    return [npm_executable, *NPM_SYNC_CONTRACT]


def _npm_projects(source: Path) -> tuple[Path, ...]:
    projects: list[Path] = []
    for root, directories, files in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        directories[:] = [
            name
            for name in directories
            if name not in {".git", ".grok-output", ".grok-worker", "node_modules"}
            and not (root_path / name).is_symlink()
        ]
        if "package-lock.json" not in files:
            continue
        lock = root_path / "package-lock.json"
        if lock.is_symlink():
            continue
        project = root_path
        package = project / "package.json"
        if package.is_file() and not package.is_symlink():
            projects.append(project)
    return tuple(sorted(projects, key=lambda path: path.relative_to(source).as_posix()))


def _npm_fingerprint(project: Path) -> str:
    digest = hashlib.sha256()
    for name in ("package.json", "package-lock.json"):
        digest.update(name.encode())
        digest.update(_hash_file(project / name).encode())
    digest.update(" ".join(NPM_SYNC_CONTRACT).encode())
    return digest.hexdigest()


def _npm_ready(project: Path, fingerprint: str) -> bool:
    marker = project / "node_modules" / NPM_READY_MARKER_NAME
    try:
        return (
            marker.is_file()
            and not marker.is_symlink()
            and marker.read_text(encoding="utf-8").strip() == fingerprint
        )
    except OSError:
        return False


def _prepare_npm_projects(source: Path, shared_root: Path) -> tuple[str, ...]:
    projects = _npm_projects(source)
    if not projects:
        return ()
    npm = which("npm")
    if not npm:
        raise DepsError("package-lock.json present but npm not found on PATH")
    cache = shared_root / "npm"
    cache.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["NPM_CONFIG_CACHE"] = str(cache)
    prepared: list[str] = []
    for project in projects:
        relative = project.relative_to(source).as_posix() or "."
        fingerprint = _npm_fingerprint(project)
        if not _npm_ready(project, fingerprint):
            try:
                subprocess.run(
                    build_npm_sync_cmd(npm),
                    cwd=project,
                    check=True,
                    capture_output=True,
                    text=True,
                    env=env,
                    startupinfo=hidden_startup_info(),
                    creationflags=(
                        int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
                    ),
                )
            except subprocess.CalledProcessError as exc:
                detail = exc.stderr or exc.stdout or str(exc)
                raise DepsError(f"npm ci failed in {relative}: {detail}") from exc
            marker = project / "node_modules" / NPM_READY_MARKER_NAME
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(fingerprint + "\n", encoding="utf-8")
        prepared.append(relative)
    return tuple(prepared)


def _interpreter_present(venv: Path) -> bool:
    candidates = (
        venv / "Scripts" / "python.exe",
        venv / "bin" / "python",
        venv / "bin" / "python3",
    )
    for p in candidates:
        if p.is_file() and not p.is_symlink():
            return True
        # allow symlink to real interpreter inside shared env
        if p.is_file() or (p.is_symlink() and p.exists()):
            return True
    return False


def _marker_payload(fingerprint: str) -> dict[str, object]:
    return {
        "fingerprint": fingerprint,
        "sync_contract": list(SYNC_CONTRACT),
    }


def _ready_marker_valid(venv: Path, fingerprint: str) -> bool:
    marker = venv / READY_MARKER_NAME
    if not marker.is_file() or marker.is_symlink():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("fingerprint") != fingerprint:
        return False
    if data.get("sync_contract") != list(SYNC_CONTRACT):
        return False
    return _interpreter_present(venv)


def _write_ready_marker(venv: Path, fingerprint: str) -> None:
    marker = venv / READY_MARKER_NAME
    tmp = venv / f".{READY_MARKER_NAME}.tmp.{os.getpid()}"
    payload = json.dumps(_marker_payload(fingerprint), sort_keys=True, indent=2) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    atomic_replace(tmp, marker)


def prepare_shared_env(
    source: Path,
    shared_root: Path,
    *,
    allow_unpinned: bool = False,
) -> dict[str, str]:
    """Prepare locked npm projects and a fingerprint-keyed shared Python env.

    npm packages are installed into their disposable clone from a shared download
    cache. Under the Python fingerprint lock, reuse a valid shared interpreter or
    run the single frozen uv command and write its ready marker.
    """
    source = source.resolve()
    shared_root = shared_root.resolve()
    exports: dict[str, str] = {}
    npm_projects = _prepare_npm_projects(source, shared_root)
    if npm_projects:
        exports["GROK_WORKER_NPM_PROJECTS"] = os.pathsep.join(npm_projects)
    has_inputs = any(
        (source / n).is_file()
        for n in ("uv.lock", "pyproject.toml", "requirements.txt", "requirements.lock")
    )
    if not has_inputs:
        return exports
    has_lock = (source / "uv.lock").is_file()
    if not has_lock:
        if not allow_unpinned:
            raise DepsError(
                "dependency inputs present but no uv.lock; pass explicit "
                "allow_unpinned/opt-in or add a lockfile (refusing unpinned sync)"
            )
        raise DepsError("unpinned sync is not supported in this lifecycle build")
    if which("uv") is None:
        raise DepsError("uv not found on PATH")

    fp = compute_fingerprint(source)
    uv_cache, venv, _ = shared_paths(shared_root, fp)
    with fingerprint_lock(shared_root, fp):
        if _ready_marker_valid(venv, fp):
            pass  # reuse
        else:
            uv_cache.mkdir(parents=True, exist_ok=True)
            venv.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["UV_CACHE_DIR"] = str(uv_cache)
            env["UV_PROJECT_ENVIRONMENT"] = str(venv)
            cmd = build_uv_sync_cmd(source, has_lock=True)
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    capture_output=True,
                    text=True,
                    env=env,
                    startupinfo=hidden_startup_info(),
                    creationflags=(
                        int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
                    ),
                )
            except subprocess.CalledProcessError as exc:
                raise DepsError(exc.stderr or str(exc)) from exc
            if not _interpreter_present(venv):
                raise DepsError(f"uv sync completed but interpreter missing in shared env: {venv}")
            _write_ready_marker(venv, fp)

    clone_src = source.resolve()
    exports.update(
        {
            "UV_CACHE_DIR": str(uv_cache),
            "UV_PROJECT_ENVIRONMENT": str(venv),
            "PYTHONPATH": f"{clone_src}{os.pathsep}{clone_src / 'src'}",
            "GROK_WORKER_DEPS_FINGERPRINT": fp,
        }
    )
    return exports


def worker_env_exports(env_vars: dict[str, str]) -> str:
    """Return a stable prompt contract for the preconfigured dependency env."""
    if "UV_PROJECT_ENVIRONMENT" in env_vars:
        lines = [
            "# Shared dependency contract (MANDATORY):",
            "#   Always use: uv run --no-sync <command>",
            "#   Never: uv sync / pip install inside the clone",
            "#   Never create clone-local .venv",
        ]
        lines.append("#   Dependency paths are already configured in the process environment")
    elif not env_vars.get("GROK_WORKER_NPM_PROJECTS"):
        lines = [
            "# Dependency preparation is disabled (MANDATORY):",
            "#   Do not run uv, uv run, uv sync, pip, or any environment creator",
            "#   Use only pre-existing system tools or an explicitly supplied absolute interpreter",
            "#   Never create clone-local .venv",
        ]
    else:
        lines = ["# Locked dependency contract (MANDATORY):"]
    if env_vars.get("GROK_WORKER_NPM_PROJECTS"):
        lines.extend(
            [
                "# Locked npm dependencies are already installed in the clone.",
                "#   Use repository package scripts; do not run npm install/ci again.",
            ]
        )
    return "\n".join(lines) + "\n"


def detect_clone_local_env(clone: Path) -> list[str]:
    """Return names of clone-local Python environments (failure signal)."""
    found: list[str] = []
    if not clone.is_dir():
        return found
    for child in clone.iterdir():
        if child.is_symlink():
            continue
        name = child.name
        if name == ".venv" or name.startswith(".venv-") or name == "venv":
            if child.is_dir():
                found.append(name)
    return found
