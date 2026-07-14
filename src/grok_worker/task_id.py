"""Strict task-id validation before any path join."""

from __future__ import annotations

import re

from grok_worker.constants import MAX_TASK_ID_LEN

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}[A-Za-z0-9]$|^[A-Za-z0-9]$")


class TaskIdError(ValueError):
    """Invalid task identifier."""


def validate_task_id(task_id: str) -> str:
    """Validate a single-component task id.

    Allowlist: letters, digits, ``.``, ``_``, ``-``.
    Rejects empty, overlong, leading dot, ``..``, slashes, and path separators.
    """
    if not isinstance(task_id, str) or not task_id:
        raise TaskIdError("task id must be a nonempty string")
    if len(task_id) > MAX_TASK_ID_LEN:
        raise TaskIdError(f"task id exceeds {MAX_TASK_ID_LEN} characters")
    if task_id.startswith("."):
        raise TaskIdError("task id must not start with a dot")
    if ".." in task_id:
        raise TaskIdError("task id must not contain '..'")
    if "/" in task_id or "\\" in task_id or "\0" in task_id:
        raise TaskIdError("task id must be a single path component")
    if not _TASK_ID_RE.fullmatch(task_id):
        raise TaskIdError(
            "task id may only contain letters, digits, '.', '_', '-' "
            "and must start/end with alphanumeric"
        )
    return task_id
