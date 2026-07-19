"""Source disclosure planning: dirty allowlist, secret gates, symlink containment.

Inspects only material that would be disclosed. Never logs secret values, file
contents, prompts, tokens, or environment maps. Clean committed Git content is
trusted; scanning focuses on explicitly included dirty / non-Git material.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from grok_worker.constants import DISCLOSURE_FILE_NAME, EXCLUDE_DIR_NAMES, META_DIR_NAME
from grok_worker.models import atomic_write_text
from grok_worker.paths import meta_dir

# High-confidence, low-false-positive path / content rules (dirty / non-git only).
_SENSITIVE_PATH_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)\.env(\.|$)", re.IGNORECASE),
    re.compile(r"(^|/)(\.env\.[^/]+)$", re.IGNORECASE),
    re.compile(r"(^|/)(id_rsa|id_ed25519|id_ecdsa)(\.pub)?$", re.IGNORECASE),
    re.compile(r"(^|/)(credentials|secrets?)\.(json|ya?ml|toml|env)$", re.IGNORECASE),
    re.compile(r"(^|/)(\.npmrc|\.pypirc|\.netrc)$", re.IGNORECASE),
    re.compile(
        r"(^|/)(aws_credentials|gcloud/application_default_credentials\.json)$",
        re.IGNORECASE,
    ),
    re.compile(r"(^|/).*\.(pem|p12|pfx|key)$", re.IGNORECASE),
)

# Conventional template basenames exempt from *path-only* refusal (still content-scanned).
_ENV_TEMPLATE_BASENAMES: frozenset[str] = frozenset(
    {
        ".env.example",
        ".env.sample",
        ".env.template",
        ".env.dist",
    }
)

_PRIVATE_KEY_HEADER_RE = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
_TOKEN_LINE_RE = re.compile(
    rb"(?i)(?:api[_-]?key|secret|token|password|authorization)\s*[:=]\s*(?:"
    rb"['\"][A-Za-z0-9_\-]{16,}|"
    rb"(?=[A-Za-z0-9_\-]{20,}(?:\s|$))(?=[A-Za-z0-9_\-]*[\d-])[A-Za-z0-9_\-]{20,}"
    rb")"
)

# Content scan size cap (bytes) for dirty candidates only.
_CONTENT_SCAN_LIMIT = 64 * 1024


class DisclosureError(RuntimeError):
    """Raised when disclosure policy refuses materialization."""

    def __init__(
        self,
        message: str,
        *,
        reason_codes: Sequence[str] | None = None,
        blocked_items: Sequence[tuple[str, str]] | None = None,
    ) -> None:
        self.reason_codes = list(reason_codes or [])
        # Relative paths and rule codes only. Never include matched values/content.
        self.blocked_items = [
            {"path": str(path), "reason_code": str(reason)}
            for path, reason in (blocked_items or ())
        ]
        super().__init__(message)


@dataclass
class DisclosureSummary:
    source_kind: str  # git | non-git | prompt-only
    base_sha: str | None = None
    included_dirty_paths: list[str] = field(default_factory=list)
    included_dirty_count: int = 0
    excluded_count: int = 0
    blocked_count: int = 0
    reason_codes: list[str] = field(default_factory=list)
    risk_decision: str = "allow"  # allow | refuse
    include_dirty: bool = False
    allowlist: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write(self, clone: Path) -> Path:
        path = meta_dir(clone) / DISCLOSURE_FILE_NAME
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        atomic_write_text(path, payload)
        return path


def normalize_repo_rel(path: str) -> str:
    """Normalize a repository-relative path; reject abs, .., NUL, .git, etc."""
    if path is None:
        raise DisclosureError("empty dirty path", reason_codes=["empty_path"])
    raw = str(path)
    if not raw or raw.strip() == "":
        raise DisclosureError("empty dirty path", reason_codes=["empty_path"])
    if "\0" in raw:
        raise DisclosureError("dirty path contains NUL", reason_codes=["nul_in_path"])
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise DisclosureError(
            "dirty path must be repository-relative (absolute paths rejected)",
            reason_codes=["absolute_path"],
        )
    # Normalize separators without resolving against the filesystem yet.
    cleaned = raw.replace("\\", "/").strip("/")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if not parts:
        raise DisclosureError("empty dirty path", reason_codes=["empty_path"])
    if any(p == ".." for p in parts):
        raise DisclosureError(
            "dirty path must not contain '..'",
            reason_codes=["path_traversal"],
        )
    if parts[0] == ".git" or ".git" in parts:
        raise DisclosureError(
            "dirty path must not target .git or internal managed paths",
            reason_codes=["git_internal"],
        )
    if parts[0] in EXCLUDE_DIR_NAMES or parts[0] == META_DIR_NAME:
        raise DisclosureError(
            f"dirty path targets managed/excluded directory: {parts[0]}",
            reason_codes=["managed_path"],
        )
    return "/".join(parts)


def _basename(rel: str) -> str:
    return Path(rel).name


def _is_env_template(rel: str) -> bool:
    return _basename(rel).lower() in {b.lower() for b in _ENV_TEMPLATE_BASENAMES}


def _is_sensitive_rel(rel: str) -> str | None:
    """Return a reason code for path-shaped secrets, or None if not path-blocked.

    Conventional template basenames (.env.example, .env.sample, .env.template,
    .env.dist) are exempt from path-only refusal; content is still scanned.
    """
    if _is_env_template(rel):
        return None
    for pattern in _SENSITIVE_PATH_RES:
        if pattern.search(rel):
            return "sensitive_path"
    return None


def _scan_content_for_secrets(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        data = path.read_bytes()[:_CONTENT_SCAN_LIMIT]
    except OSError as exc:
        # Fail closed: cannot read selected material → refuse without content.
        raise DisclosureError(
            "cannot read selected material for secret scan (contents not logged)",
            reason_codes=["material_read_error"],
        ) from exc
    if _PRIVATE_KEY_HEADER_RE.search(data):
        return "private_key_header"
    if _TOKEN_LINE_RE.search(data):
        return "credential_shaped_token"
    return None


def _path_is_absent(source: Path, rel: str) -> bool:
    """True when path does not exist as file/dir/symlink (tracked deletion is safe)."""
    fp = source / rel
    try:
        if fp.is_symlink():
            return False
        return not fp.exists()
    except OSError:
        return False


def _symlink_escapes(source: Path, rel: str) -> bool:
    """True when file or directory symlink is absolute or resolves outside source."""
    src_f = source / rel
    if not src_f.is_symlink():
        return False
    try:
        target = os.readlink(src_f)
    except OSError:
        return True
    if target.startswith("/") or (len(target) > 1 and target[1] == ":"):
        return True
    try:
        source_res = source.resolve()
        # Resolve via parent of link without requiring the target to exist.
        resolved = (src_f.parent / target).resolve()
        resolved.relative_to(source_res)
    except (OSError, ValueError):
        return True
    return False


def _material_risk(source: Path, rel: str) -> str | None:
    """Return a disclosure risk for the exact material currently at *rel*."""
    if _path_is_absent(source, rel):
        return None
    if _symlink_escapes(source, rel):
        return "symlink_escape"
    partial = ""
    for part in Path(rel).parts[:-1]:
        partial = f"{partial}/{part}" if partial else part
        if (source / partial).is_symlink() and _symlink_escapes(source, partial):
            return "symlink_escape"
    sensitive = _is_sensitive_rel(rel)
    if sensitive:
        return sensitive
    path = source / rel
    if path.is_file() and not path.is_symlink():
        return _scan_content_for_secrets(path)
    return None


def validate_materialized_paths(root: Path, paths: Sequence[str]) -> None:
    """Recheck the exact clone bytes after dirty materialization.

    This closes the scan/copy race: source files may change after planning, but
    only the already-isolated clone bytes can reach the worker.
    """
    reasons = [risk for rel in paths if (risk := _material_risk(root, rel))]
    if reasons:
        raise DisclosureError(
            "materialized dirty snapshot refused: high-confidence sensitive "
            "path/content or symlink escape; secret values are never logged",
            reason_codes=sorted(set(reasons)),
        )


def _git_is_ignored(repo: Path, rel: str) -> bool:
    """Return whether *rel* is ignored.

    Fail closed on check-ignore errors (nonzero other than 0/1): raise without
    including file contents or secret values.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", "--", rel],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise DisclosureError(
            "git check-ignore failed (contents not logged)",
            reason_codes=["check_ignore_error"],
        ) from exc
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise DisclosureError(
        "git check-ignore failed (contents not logged)",
        reason_codes=["check_ignore_error"],
    )


def _git_untracked_nonignored(repo: Path) -> list[str]:
    """Untracked paths respecting standard Git excludes (never ignored files)."""
    try:
        raw = subprocess.check_output(
            ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"]
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise DisclosureError(
            "git untracked listing failed (contents not logged)",
            reason_codes=["untracked_list_error"],
        ) from exc
    out: list[str] = []
    for b in raw.split(b"\0"):
        if not b:
            continue
        rel = b.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        if any(p in EXCLUDE_DIR_NAMES for p in Path(rel).parts):
            continue
        if Path(rel).name.startswith("prompt-"):
            continue
        out.append(rel)
    return out


def _git_dirty_tracked_paths(repo: Path) -> list[str]:
    """Tracked modified/staged/deleted paths relative to HEAD (including renames)."""
    names: set[str] = set()
    # name-status -z: for renames, records are "R100\0old\0new\0" (status, then two paths).
    for args in (
        ["git", "-C", str(repo), "diff", "--name-status", "-z", "HEAD"],
        ["git", "-C", str(repo), "diff", "--name-status", "-z", "--cached"],
    ):
        try:
            blob = subprocess.check_output(args)
        except subprocess.CalledProcessError:
            continue
        parts = [p for p in blob.split(b"\0") if p]
        i = 0
        while i < len(parts):
            status = parts[i].decode("utf-8", errors="replace")
            i += 1
            if not status:
                continue
            code = status[0] if status else ""
            if code in {"R", "C"} and i + 1 < len(parts):
                old = parts[i].decode("utf-8", errors="surrogateescape").replace("\\", "/")
                new = parts[i + 1].decode("utf-8", errors="surrogateescape").replace("\\", "/")
                names.add(old)
                names.add(new)
                i += 2
            elif i < len(parts):
                rel = parts[i].decode("utf-8", errors="surrogateescape").replace("\\", "/")
                names.add(rel)
                i += 1
    return sorted(names)


def inventory_git_dirty(source: Path) -> tuple[list[str], list[str]]:
    """Return (tracked_or_untracked_nonignored, ignored_untracked_seen_sample)."""
    tracked = _git_dirty_tracked_paths(source)
    untracked = _git_untracked_nonignored(source)
    # Sample ignored untracked for migration messaging (paths only, no content).
    try:
        raw_all = subprocess.check_output(
            ["git", "-C", str(source), "ls-files", "--others", "-z"]
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise DisclosureError(
            "git untracked listing failed (contents not logged)",
            reason_codes=["untracked_list_error"],
        ) from exc
    ignored_sample: list[str] = []
    nonignored = set(untracked)
    for b in raw_all.split(b"\0"):
        if not b:
            continue
        rel = b.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        if rel in nonignored:
            continue
        if _git_is_ignored(source, rel):
            ignored_sample.append(rel)
            if len(ignored_sample) >= 20:
                break
    combined = sorted(set(tracked) | set(untracked))
    return combined, ignored_sample


def plan_disclosure(
    source: Path | None,
    *,
    include_dirty: bool = False,
    dirty_allowlist: Sequence[str] | None = None,
    prompt_only: bool = False,
    is_git: bool | None = None,
) -> DisclosureSummary:
    """Plan disclosure before clone/deps. Never returns secret values."""
    if prompt_only:
        if include_dirty or dirty_allowlist:
            raise DisclosureError(
                "prompt-only mode rejects dirty/source materialization flags",
                reason_codes=["prompt_only_incompatible_dirty"],
            )
        return DisclosureSummary(
            source_kind="prompt-only",
            risk_decision="allow",
            reason_codes=["prompt_only"],
        )

    if source is None:
        raise DisclosureError("source is required unless prompt-only", reason_codes=["no_source"])

    source = Path(source)
    if not source.exists():
        raise DisclosureError("source not found", reason_codes=["source_missing"])

    git = is_git if is_git is not None else (source / ".git").exists()
    allowlist = [normalize_repo_rel(p) for p in (dirty_allowlist or ())]
    summary = DisclosureSummary(
        source_kind="git" if git else "non-git",
        include_dirty=bool(include_dirty or allowlist),
        allowlist=list(allowlist),
    )

    if not git:
        # Non-git trees are fully materialised; scan file and directory symlinks
        # for escapes, and high-confidence risks without dumping contents.
        blocked = 0
        reasons: list[str] = []
        nongit_blocked_items: list[tuple[str, str]] = []
        for root, dirs, files in os.walk(source, followlinks=False):
            # Inspect directory symlinks before pruning (they would be walked otherwise).
            for d in list(dirs):
                rel_d = str((Path(root) / d).relative_to(source)).replace("\\", "/")
                dp = source / rel_d
                if dp.is_symlink() and _symlink_escapes(source, rel_d):
                    blocked += 1
                    reasons.append("symlink_escape")
                    nongit_blocked_items.append((rel_d, "symlink_escape"))
            dirs[:] = [
                d
                for d in sorted(dirs)
                if d not in EXCLUDE_DIR_NAMES and not (Path(root) / d).is_symlink()
            ]
            for name in files:
                rel = str((Path(root) / name).relative_to(source)).replace("\\", "/")
                fp = source / rel
                if fp.is_symlink() and _symlink_escapes(source, rel):
                    blocked += 1
                    reasons.append("symlink_escape")
                    nongit_blocked_items.append((rel, "symlink_escape"))
                    continue
                hit = _is_sensitive_rel(rel)
                if hit:
                    content_hit = _scan_content_for_secrets(fp)
                    if content_hit:
                        blocked += 1
                        reasons.append(content_hit)
                        nongit_blocked_items.append((rel, content_hit))
                        continue
                    # Path-shaped without content hit: still refuse non-git secrets.
                    blocked += 1
                    reasons.append(hit)
                    nongit_blocked_items.append((rel, hit))
                    continue
                # Templates and other files: content scan when regular file.
                if fp.is_file() and not fp.is_symlink():
                    content_hit = _scan_content_for_secrets(fp)
                    if content_hit:
                        blocked += 1
                        reasons.append(content_hit)
                        nongit_blocked_items.append((rel, content_hit))
        if blocked:
            summary.blocked_count = blocked
            summary.reason_codes = sorted(set(reasons))
            summary.risk_decision = "refuse"
            raise DisclosureError(
                "non-git source refused: high-confidence sensitive or escaping material "
                f"(reason_codes={summary.reason_codes}); secret values are not logged",
                reason_codes=summary.reason_codes,
                blocked_items=nongit_blocked_items,
            )
        summary.risk_decision = "allow"
        return summary

    dirty_paths, ignored_sample = inventory_git_dirty(source)
    if not dirty_paths:
        summary.risk_decision = "allow"
        summary.reason_codes = ["clean_head"]
        return summary

    summary.include_dirty = True
    if not allowlist:
        summary.reason_codes.append("auto_safe_dirty_snapshot")
        if ignored_sample:
            summary.reason_codes.append("ignored_excluded")
            summary.excluded_count += len(ignored_sample)
    else:
        dirty_set = set(dirty_paths)
        stale_count = len([rel for rel in allowlist if rel not in dirty_set])
        summary.excluded_count += stale_count
        if stale_count:
            summary.reason_codes.append("stale_allowlist_ignored")
        summary.reason_codes.append("legacy_allowlist_nonfiltering")

    # v0.5 snapshots every safe dirty path. The old allowlist is accepted for
    # script compatibility but no longer creates an incomplete worker baseline.
    candidates = list(dirty_paths)

    included: list[str] = []
    blocked = 0
    git_blocked_items: list[tuple[str, str]] = []
    for rel in candidates:
        if _git_is_ignored(source, rel):
            summary.excluded_count += 1
            summary.reason_codes.append("ignored")
            continue
        # Already-tracked deletions: absent paths are safe; skip path/content scan.
        if _path_is_absent(source, rel):
            included.append(rel)
            continue
        risk = _material_risk(source, rel)
        if risk:
            blocked += 1
            summary.reason_codes.append(risk)
            git_blocked_items.append((rel, risk))
            continue
        included.append(rel)

    summary.included_dirty_paths = sorted(set(included))
    summary.included_dirty_count = len(summary.included_dirty_paths)
    summary.blocked_count = blocked
    summary.reason_codes = sorted(set(summary.reason_codes))

    if blocked:
        summary.risk_decision = "refuse"
        raise DisclosureError(
            "dirty materialization refused: high-confidence sensitive path/content or "
            f"symlink escape (reason_codes={summary.reason_codes}). "
            "Use --include-dirty-path with only safe relative paths; "
            "secret values are never logged. Ignored files are never copied.",
            reason_codes=summary.reason_codes,
            blocked_items=git_blocked_items,
        )

    summary.risk_decision = "allow"
    return summary


def write_disclosure_summary(clone: Path, summary: DisclosureSummary) -> Path:
    return summary.write(clone)


def disclosure_preflight(source: Path) -> dict[str, Any]:
    """Return one value-free disclosure scan report without creating a clone."""
    try:
        summary = plan_disclosure(source)
    except DisclosureError as exc:
        return {
            "allowed": False,
            "blocked_count": len(exc.blocked_items),
            "blocked": exc.blocked_items,
            "reason_codes": sorted(set(exc.reason_codes)),
            "values_exposed": False,
        }
    return {
        "allowed": True,
        "blocked_count": 0,
        "blocked": [],
        "reason_codes": summary.reason_codes,
        "included_dirty_count": summary.included_dirty_count,
        "excluded_count": summary.excluded_count,
        "source_kind": summary.source_kind,
        "values_exposed": False,
    }
