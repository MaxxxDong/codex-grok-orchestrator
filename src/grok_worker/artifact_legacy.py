"""Read and verify v1 manifest artifacts retained for GC compatibility."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class LegacyArtifactError(RuntimeError):
    """A v1 artifact manifest is malformed or does not match disk."""


REQUIRED = frozenset({"changes.patch", "exit-status.json", "lifecycle.json"})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
        for name in sorted(names):
            path = Path(current) / name
            if path.is_file() and not path.is_symlink():
                files.append(path)
    return files


def write_manifest(root: Path) -> Path:
    lines = []
    for path in _regular_files(root):
        relative = path.relative_to(root).as_posix()
        if relative != "MANIFEST.sha256":
            lines.append(f"{sha256_file(path)}  {relative}")
    manifest = root / "MANIFEST.sha256"
    manifest.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return manifest


def verify_manifest(root: Path, *, require_agent_log: bool = False) -> None:
    manifest = root / "MANIFEST.sha256"
    if not manifest.is_file() or manifest.is_symlink():
        raise LegacyArtifactError(f"missing MANIFEST.sha256 in {root}")
    top_files = {path.name for path in root.iterdir() if path.is_file() and not path.is_symlink()}
    for name in REQUIRED:
        if name not in top_files:
            raise LegacyArtifactError(f"missing required artifact file: {name}")
    if require_agent_log and "agent.log" not in top_files:
        raise LegacyArtifactError("missing required agent.log for acpx run")

    listed: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        if len(parts) != 2:
            raise LegacyArtifactError(f"malformed manifest line: {line!r}")
        expected, relative = parts
        path = Path(relative)
        if (
            relative in listed
            or relative == "MANIFEST.sha256"
            or path.is_absolute()
            or ".." in path.parts
        ):
            raise LegacyArtifactError(f"invalid manifest path: {relative}")
        listed[relative] = expected

    actual = {
        path.relative_to(root).as_posix(): path
        for path in _regular_files(root)
        if path.relative_to(root).as_posix() != "MANIFEST.sha256"
    }
    extras = sorted(set(actual) - set(listed))
    missing = sorted(set(listed) - set(actual))
    if extras:
        raise LegacyArtifactError(f"extra artifact file(s) not listed: {extras}")
    if missing:
        raise LegacyArtifactError(f"manifest entry missing: {missing}")
    for relative, expected in listed.items():
        if sha256_file(actual[relative]) != expected:
            raise LegacyArtifactError(f"manifest hash mismatch for {relative}")
