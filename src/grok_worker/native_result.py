"""Native JSON Schema structured-result capture for Grok Build headless runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from grok_worker.constants import RESULT_SCHEMA_VERSION
from grok_worker.models import WorkerResult, atomic_write_text
from grok_worker.result_schema import (
    ResultError,
    parse_result_dict,
    result_path,
    validate_verification_files,
)

# Marker injected into the dynamic task suffix (never into the stable prefix).
NATIVE_RESULT_CAPTURE_MARKER = "NATIVE_STRUCTURED_RESULT_CAPTURE_V1"

NATIVE_RESULT_CAPTURE_GUIDANCE = f"""--- {NATIVE_RESULT_CAPTURE_MARKER} ---
Native structured-output mode is active. Emit the final WorkerResult as the
constrained JSON Schema response for this run. The lifecycle runner validates
that object and atomically writes `.grok-output/result.json` itself.

You must still:
- write `.grok-worker/progress.json` phase updates
- create real verification logs under `.grok-output/verification/`
- leave an inspectable workspace diff when the role requires it

Do **not** create or rewrite `.grok-output/result.json` yourself in this mode.
Patch capture and `worker.log` remain runner-generated. ACP/legacy paths without
this marker still require the model to write `result.json` on disk.
"""


def worker_result_json_schema() -> dict[str, Any]:
    """JSON Schema passed to `grok --json-schema` for final structured output."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "task_completed",
            "status",
            "summary",
            "findings",
            "verification",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": RESULT_SCHEMA_VERSION},
            "task_completed": {"type": "boolean"},
            "status": {
                "type": "string",
                "enum": ["completed", "failed", "partial"],
            },
            "summary": {"type": "string", "minLength": 1},
            "findings": {
                "type": "array",
                "items": {"type": "object"},
            },
            "verification": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["command", "exit_code", "log_path"],
                    "properties": {
                        "command": {"type": "string", "minLength": 1},
                        "exit_code": {"type": "integer"},
                        "log_path": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
    }


def json_schema_cli_argument() -> str:
    return json.dumps(worker_result_json_schema(), separators=(",", ":"), sort_keys=True)


def _as_result_dict(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    keys = {
        "schema_version",
        "task_completed",
        "status",
        "summary",
        "verification",
    }
    if keys.issubset(payload.keys()):
        return {str(k): v for k, v in payload.items()}
    for nest_key in ("result", "structured_output", "output", "data"):
        nested = payload.get(nest_key)
        if isinstance(nested, dict) and keys.issubset(nested.keys()):
            return {str(k): v for k, v in nested.items()}
    return None


def extract_structured_result_from_text(text: str) -> dict[str, object]:
    """Parse the final WorkerResult object from native JSON stdout/log text."""
    if not text or not text.strip():
        raise ResultError("native structured output empty")

    # Prefer whole-document JSON (typical --output-format json).
    try:
        whole: object = json.loads(text)
    except json.JSONDecodeError:
        whole = None
    if whole is not None:
        candidate = _as_result_dict(whole)
        if candidate is not None:
            return candidate
        if isinstance(whole, dict):
            # Grok sometimes wraps model JSON in a text field.
            for key in ("text", "message", "content", "agent_output"):
                nested = whole.get(key)
                if isinstance(nested, str) and nested.strip():
                    try:
                        return extract_structured_result_from_text(nested)
                    except ResultError:
                        pass

    # JSON-lines: take the last parseable WorkerResult-shaped object.
    decoder = json.JSONDecoder()
    last: dict[str, object] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            payload, _ = decoder.raw_decode(line)
        except json.JSONDecodeError:
            continue
        candidate = _as_result_dict(payload)
        if candidate is not None:
            last = candidate
    if last is not None:
        return last

    # Embedded object scan (log prefixes / pretty multi-line dumps).
    for index in (pos for pos in range(len(text) - 1, -1, -1) if text[pos] == "{"):
        try:
            payload, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        candidate = _as_result_dict(payload)
        if candidate is not None:
            return candidate

    raise ResultError("native structured output incompatible: no WorkerResult-shaped JSON found")


def persist_native_structured_result(
    clone: Path,
    log_text: str,
    *,
    mode: str,
) -> WorkerResult:
    """Validate model JSON Schema output and atomically write canonical result.json."""
    raw = extract_structured_result_from_text(log_text)
    result = parse_result_dict(raw)
    if result.schema_version != RESULT_SCHEMA_VERSION:
        raise ResultError(
            f"unsupported result schema_version {result.schema_version}; "
            f"expected {RESULT_SCHEMA_VERSION}"
        )
    # Implementation still requires on-disk verification logs; validate them.
    if mode == "implementation":
        validate_verification_files(clone, result)
    path = result_path(clone)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ResultError(f"refusing symlink result path: {path}")
    payload = json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, payload)
    return result


__all__ = [
    "NATIVE_RESULT_CAPTURE_GUIDANCE",
    "NATIVE_RESULT_CAPTURE_MARKER",
    "extract_structured_result_from_text",
    "json_schema_cli_argument",
    "persist_native_structured_result",
    "worker_result_json_schema",
]
