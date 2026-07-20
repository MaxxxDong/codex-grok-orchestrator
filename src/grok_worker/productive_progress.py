"""Productive-progress detection distinct from lease liveness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from grok_worker.completion_events import emit_completion_event
from grok_worker.constants import OUTPUT_DIR_NAME, RESULT_FILE_NAME
from grok_worker.models import atomic_write_text, dt_from_iso, dt_to_iso, utc_now
from grok_worker.paths import meta_dir
from grok_worker.status import PROGRESS_STEPS, workspace_activity_at

# Defaults are conservative: warn, never kill solely for this signal.
DEFAULT_STALL_TURNS = 8
DEFAULT_STALL_SECONDS = 900.0
PROGRESS_STATE_FILE = "productive-progress.json"


@dataclass
class ProductiveSnapshot:
    workspace_mtime: float | None
    progress_step: str | None
    result_mtime: float | None
    verification_mtime: float | None
    verification_count: int
    model_turns: int | None

    def fingerprint(self) -> str:
        # Model turns are liveness, not productive progress — exclude them.
        return "|".join(
            [
                str(self.workspace_mtime or 0),
                self.progress_step or "",
                str(self.result_mtime or 0),
                str(self.verification_mtime or 0),
                str(self.verification_count),
            ]
        )


@dataclass
class ProductiveProgressState:
    last_fingerprint: str
    last_productive_at: str
    stall_attention_emitted: bool
    observed_turns_without_progress: int
    model_turns_at_last_progress: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_fingerprint": self.last_fingerprint,
            "last_productive_at": self.last_productive_at,
            "stall_attention_emitted": self.stall_attention_emitted,
            "observed_turns_without_progress": self.observed_turns_without_progress,
            "model_turns_at_last_progress": self.model_turns_at_last_progress,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProductiveProgressState:
        return cls(
            last_fingerprint=str(data.get("last_fingerprint", "")),
            last_productive_at=str(data.get("last_productive_at") or dt_to_iso(utc_now())),
            stall_attention_emitted=bool(data.get("stall_attention_emitted", False)),
            observed_turns_without_progress=int(data.get("observed_turns_without_progress", 0)),
            model_turns_at_last_progress=_optional_int(data.get("model_turns_at_last_progress")),
        )


def progress_state_path(clone: Path) -> Path:
    return meta_dir(clone) / PROGRESS_STATE_FILE


def _optional_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _file_mtime(path: Path) -> float | None:
    try:
        if path.is_file() and not path.is_symlink():
            return path.stat().st_mtime
    except OSError:
        return None
    return None


def _progress_step(clone: Path) -> str | None:
    path = meta_dir(clone) / "progress.json"
    try:
        if not path.is_file() or path.is_symlink():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    step = raw.get("step")
    if isinstance(step, str) and step in PROGRESS_STEPS:
        return step
    return None


def _verification_stats(clone: Path) -> tuple[float | None, int]:
    root = clone / OUTPUT_DIR_NAME / "verification"
    if not root.is_dir() or root.is_symlink():
        return None, 0
    latest: float | None = None
    count = 0
    try:
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                count += 1
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if latest is None or mtime > latest:
                    latest = mtime
    except OSError:
        return latest, count
    return latest, count


def capture_snapshot(
    clone: Path, *, model_turns: int | None = None, now: datetime | None = None
) -> ProductiveSnapshot:
    clock = now or utc_now()
    workspace = workspace_activity_at(clone, now=clock)
    workspace_mtime = workspace.timestamp() if workspace is not None else None
    ver_mtime, ver_count = _verification_stats(clone)
    return ProductiveSnapshot(
        workspace_mtime=workspace_mtime,
        progress_step=_progress_step(clone),
        result_mtime=_file_mtime(clone / OUTPUT_DIR_NAME / RESULT_FILE_NAME),
        verification_mtime=ver_mtime,
        verification_count=ver_count,
        model_turns=model_turns,
    )


def load_state(clone: Path) -> ProductiveProgressState | None:
    path = progress_state_path(clone)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return ProductiveProgressState.from_dict(raw)


def save_state(clone: Path, state: ProductiveProgressState) -> None:
    path = progress_state_path(clone)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n")


def evaluate_productive_progress(
    clone: Path,
    *,
    model_turns: int | None = None,
    stall_turns: int = DEFAULT_STALL_TURNS,
    stall_seconds: float = DEFAULT_STALL_SECONDS,
    task_id: str,
    run_id: str | None,
    dispatcher_id: str | None,
    shared_cache_root: Path | None,
    emit_attention: bool = True,
) -> dict[str, Any]:
    """Update productive-progress state; emit attention on stall without killing."""
    snap = capture_snapshot(clone, model_turns=model_turns)
    fp = snap.fingerprint()
    now = utc_now()
    state = load_state(clone)
    if state is None:
        state = ProductiveProgressState(
            last_fingerprint=fp,
            last_productive_at=dt_to_iso(now) or "",
            stall_attention_emitted=False,
            observed_turns_without_progress=0,
            model_turns_at_last_progress=model_turns,
        )
        save_state(clone, state)
        return {
            "productive": True,
            "stalled": False,
            "attention_emitted": False,
            "fingerprint": fp,
        }

    productive = fp != state.last_fingerprint
    if productive:
        state.last_fingerprint = fp
        state.last_productive_at = dt_to_iso(now) or state.last_productive_at
        state.observed_turns_without_progress = 0
        state.model_turns_at_last_progress = model_turns
        state.stall_attention_emitted = False
        save_state(clone, state)
        return {
            "productive": True,
            "stalled": False,
            "attention_emitted": False,
            "fingerprint": fp,
        }

    # No fingerprint change: count stall by turns and/or wall time.
    if model_turns is not None and state.model_turns_at_last_progress is not None:
        state.observed_turns_without_progress = max(
            0, model_turns - state.model_turns_at_last_progress
        )
    elif model_turns is not None:
        state.observed_turns_without_progress += 1
    turn_stall = state.observed_turns_without_progress >= stall_turns

    last = dt_from_iso(state.last_productive_at) or now
    elapsed = (now - last).total_seconds()
    time_stall = elapsed >= stall_seconds
    stalled = turn_stall or time_stall
    attention_emitted = False
    if stalled and not state.stall_attention_emitted and emit_attention:
        emit_completion_event(
            task_id=task_id,
            state="running",
            artifact_path=None,
            shared_cache_root=shared_cache_root,
            run_id=run_id,
            dispatcher_id=dispatcher_id,
            kind="attention",
            artifact_ready=False,
            attention_required=True,
            reason_code="no_productive_progress",
        )
        state.stall_attention_emitted = True
        attention_emitted = True
    save_state(clone, state)
    return {
        "productive": False,
        "stalled": stalled,
        "attention_emitted": attention_emitted,
        "fingerprint": fp,
        "elapsed_seconds": elapsed,
        "turns_without_progress": state.observed_turns_without_progress,
    }


def parse_model_turns_from_log(log_text: str) -> int | None:
    """Extract model turn/call count when native JSON exposes it; else None."""
    if not log_text:
        return None
    from grok_worker.metrics import extract_token_metrics_from_text

    metrics = extract_token_metrics_from_text(log_text)
    return metrics.model_calls
