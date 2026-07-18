"""Root-owned activity lease used instead of a fixed launch-time worker lifetime."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from grok_worker.constants import (
    DEFAULT_HARD_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
    LEASE_FILE_NAME,
    LEASE_LOCK_NAME,
    LEASE_POLL_SECONDS,
    OUTPUT_DIR_NAME,
    RESULT_FILE_NAME,
)
from grok_worker.grok_profile import worker_grok_home
from grok_worker.locks import FileLock
from grok_worker.models import atomic_write_text, dt_to_iso, utc_now
from grok_worker.paths import meta_dir
from grok_worker.status import workspace_activity_at

LEASE_SCHEMA_VERSION = 1
LEASE_TIMEOUT_EXIT_CODE = 124


class LeaseError(RuntimeError):
    """Raised when lease state or policy is invalid."""


@dataclass(frozen=True)
class LeaseState:
    schema_version: int
    idle_timeout_seconds: int
    hard_timeout_seconds: int | None
    started_at: str
    last_activity_at: str
    last_activity_source: str
    updated_at: str
    revision: int = 1

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LeaseState:
        if data.get("schema_version") != LEASE_SCHEMA_VERSION:
            raise LeaseError("unsupported lease schema")
        idle = _positive_int(data.get("idle_timeout_seconds"), "idle timeout")
        hard_raw = data.get("hard_timeout_seconds")
        hard = None if hard_raw is None else _positive_int(hard_raw, "hard timeout")
        return cls(
            schema_version=LEASE_SCHEMA_VERSION,
            idle_timeout_seconds=idle,
            hard_timeout_seconds=hard,
            started_at=_required_text(data.get("started_at"), "started_at"),
            last_activity_at=_required_text(
                data.get("last_activity_at"), "last_activity_at"
            ),
            last_activity_source=_required_text(
                data.get("last_activity_source"), "last_activity_source"
            ),
            updated_at=_required_text(data.get("updated_at"), "updated_at"),
            revision=_positive_int(data.get("revision", 1), "revision"),
        )


@dataclass(frozen=True)
class LeasedProcessResult:
    exit_code: int
    timeout_kind: str | None = None
    timeout_message: str | None = None


@dataclass(frozen=True)
class ActivityObservation:
    observed_at: datetime
    source: str


def lease_path(clone: Path) -> Path:
    return meta_dir(clone) / LEASE_FILE_NAME


def _lease_lock_path(clone: Path) -> Path:
    return meta_dir(clone) / LEASE_LOCK_NAME


@contextmanager
def _lease_lock(clone: Path) -> Iterator[None]:
    path = _lease_lock_path(clone)
    with FileLock(path):
        yield


def initialize_lease(
    clone: Path,
    *,
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT,
    hard_timeout_seconds: int | None = DEFAULT_HARD_TIMEOUT,
) -> LeaseState:
    idle = _positive_int(idle_timeout_seconds, "idle timeout")
    hard = (
        None
        if hard_timeout_seconds is None
        else _positive_int(hard_timeout_seconds, "hard timeout")
    )
    now = dt_to_iso(utc_now()) or ""
    state = LeaseState(
        schema_version=LEASE_SCHEMA_VERSION,
        idle_timeout_seconds=idle,
        hard_timeout_seconds=hard,
        started_at=now,
        last_activity_at=now,
        last_activity_source="process_start",
        updated_at=now,
    )
    with _lease_lock(clone):
        _write_lease(clone, state)
    return state


def read_lease(clone: Path) -> LeaseState:
    path = lease_path(clone)
    try:
        if path.is_symlink():
            raise LeaseError("refusing symlink activity lease")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LeaseError(f"cannot read activity lease: {exc}") from exc
    if not isinstance(raw, dict):
        raise LeaseError("activity lease must be a JSON object")
    return LeaseState.from_dict(raw)


def set_lease_policy(
    clone: Path,
    *,
    idle_timeout_seconds: int | None = None,
    hard_timeout_seconds: int | None | object = ...,
) -> LeaseState:
    """Adjust a live lease. ``hard_timeout_seconds=None`` disables the hard cap."""
    if idle_timeout_seconds is None and hard_timeout_seconds is ...:
        raise LeaseError("at least one lease setting is required")
    with _lease_lock(clone):
        current = read_lease(clone)
        idle = current.idle_timeout_seconds
        if idle_timeout_seconds is not None:
            idle = _positive_int(idle_timeout_seconds, "idle timeout")
        hard = current.hard_timeout_seconds
        if hard_timeout_seconds is not ...:
            hard = (
                None
                if hard_timeout_seconds is None
                else _positive_int(hard_timeout_seconds, "hard timeout")
            )
        now = dt_to_iso(utc_now()) or current.updated_at
        updated = LeaseState(
            schema_version=current.schema_version,
            idle_timeout_seconds=idle,
            hard_timeout_seconds=hard,
            started_at=current.started_at,
            last_activity_at=current.last_activity_at,
            last_activity_source=current.last_activity_source,
            updated_at=now,
            revision=current.revision + 1,
        )
        _write_lease(clone, updated)
        return updated


def record_activity(clone: Path, observation: ActivityObservation) -> LeaseState:
    with _lease_lock(clone):
        current = read_lease(clone)
        previous = _parse_iso(current.last_activity_at)
        observed = observation.observed_at.astimezone(UTC)
        if observed <= previous:
            return current
        now = dt_to_iso(utc_now()) or current.updated_at
        updated = LeaseState(
            schema_version=current.schema_version,
            idle_timeout_seconds=current.idle_timeout_seconds,
            hard_timeout_seconds=current.hard_timeout_seconds,
            started_at=current.started_at,
            last_activity_at=dt_to_iso(observed) or current.last_activity_at,
            last_activity_source=observation.source,
            updated_at=now,
            revision=current.revision,
        )
        _write_lease(clone, updated)
        return updated


class ActivityProbe:
    """Collect bounded worker-owned activity without reading prompts or output content."""

    def __init__(self, clone: Path, agent_log: Path | None = None) -> None:
        self.clone = clone.resolve()
        self.agent_log = agent_log
        encoded = quote(str(self.clone), safe="")
        self.session_root = worker_grok_home() / "sessions" / encoded
        self._last_workspace_scan = 0.0
        self._workspace_observation: ActivityObservation | None = None

    def latest(self, *, now: datetime | None = None) -> ActivityObservation | None:
        clock = (now or utc_now()).astimezone(UTC)
        candidates: list[ActivityObservation] = []
        for path, source in (
            (meta_dir(self.clone) / "progress.json", "progress"),
            (self.clone / OUTPUT_DIR_NAME / RESULT_FILE_NAME, "result"),
            (self.agent_log, "agent_log"),
        ):
            observed = _regular_file_activity(path, source, now=clock)
            if observed is not None:
                candidates.append(observed)
        session_observed = _session_activity(self.session_root, now=clock)
        if session_observed is not None:
            candidates.append(session_observed)

        monotonic_now = time.monotonic()
        if monotonic_now - self._last_workspace_scan >= 30.0:
            self._last_workspace_scan = monotonic_now
            workspace = workspace_activity_at(self.clone, now=clock)
            if workspace is not None:
                self._workspace_observation = ActivityObservation(workspace, "workspace")
        if self._workspace_observation is not None:
            candidates.append(self._workspace_observation)
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.observed_at)


def run_with_activity_lease(
    command: list[str],
    *,
    clone: Path,
    log: Path,
    env: dict[str, str],
    idle_timeout_seconds: int,
    hard_timeout_seconds: int | None,
    initialize: bool = True,
    poll_seconds: float = LEASE_POLL_SECONDS,
    on_start: Callable[[subprocess.Popen[Any]], None] | None = None,
) -> LeasedProcessResult:
    """Run one ACP process, renewing its inactivity deadline from real activity."""
    log.parent.mkdir(parents=True, exist_ok=True)
    if initialize:
        initialize_lease(
            clone,
            idle_timeout_seconds=idle_timeout_seconds,
            hard_timeout_seconds=hard_timeout_seconds,
        )
    probe = ActivityProbe(clone, log)
    with log.open("ab") as stream:
        process = subprocess.Popen(
            command,
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        if on_start is not None:
            on_start(process)
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                return LeasedProcessResult(int(exit_code))
            observation = probe.latest()
            if observation is not None:
                state = record_activity(clone, observation)
            else:
                state = read_lease(clone)
            clock = utc_now().astimezone(UTC)
            idle_elapsed = (clock - _parse_iso(state.last_activity_at)).total_seconds()
            hard_elapsed = (clock - _parse_iso(state.started_at)).total_seconds()
            timeout_kind: str | None = None
            if idle_elapsed >= state.idle_timeout_seconds:
                timeout_kind = "idle"
                message = (
                    "activity lease expired after "
                    f"{state.idle_timeout_seconds}s without observable progress"
                )
            elif (
                state.hard_timeout_seconds is not None
                and hard_elapsed >= state.hard_timeout_seconds
            ):
                timeout_kind = "hard"
                message = (
                    "hard worker limit reached after "
                    f"{state.hard_timeout_seconds}s"
                )
            if timeout_kind is not None:
                _append_runner_message(stream, message)
                terminate_process_tree(process)
                return LeasedProcessResult(
                    LEASE_TIMEOUT_EXIT_CODE,
                    timeout_kind=timeout_kind,
                    timeout_message=message,
                )
            try:
                exit_code = process.wait(timeout=max(0.01, poll_seconds))
            except subprocess.TimeoutExpired:
                continue
            return LeasedProcessResult(int(exit_code))


def terminate_process_tree(process: subprocess.Popen[Any], grace_seconds: float = 5.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass


def lease_summary(clone: Path, *, now: datetime | None = None) -> dict[str, object] | None:
    try:
        state = read_lease(clone)
        clock = (now or utc_now()).astimezone(UTC)
        idle_remaining = max(
            0.0,
            state.idle_timeout_seconds
            - (clock - _parse_iso(state.last_activity_at)).total_seconds(),
        )
        hard_remaining = (
            None
            if state.hard_timeout_seconds is None
            else max(
                0.0,
                state.hard_timeout_seconds
                - (clock - _parse_iso(state.started_at)).total_seconds(),
            )
        )
    except LeaseError:
        return None
    return {
        "timeout_mode": "activity_lease",
        "idle_timeout_seconds": state.idle_timeout_seconds,
        "hard_timeout_seconds": state.hard_timeout_seconds,
        "lease_remaining_seconds": idle_remaining,
        "hard_remaining_seconds": hard_remaining,
        "lease_last_activity_at": state.last_activity_at,
        "lease_activity_source": state.last_activity_source,
        "lease_revision": state.revision,
    }


def _session_activity(root: Path, *, now: datetime) -> ActivityObservation | None:
    if root.is_symlink() or not root.is_dir():
        return None
    latest: datetime | None = None
    try:
        sessions = list(root.iterdir())
    except OSError:
        return None
    for session in sessions:
        if session.is_symlink() or not session.is_dir():
            continue
        for name in ("events.jsonl", "updates.jsonl", "summary.json"):
            observed = _regular_file_activity(session / name, "grok_session", now=now)
            if observed is not None and (
                latest is None or observed.observed_at > latest
            ):
                latest = observed.observed_at
    return None if latest is None else ActivityObservation(latest, "grok_session")


def _regular_file_activity(
    path: Path | None, source: str, *, now: datetime
) -> ActivityObservation | None:
    if path is None:
        return None
    try:
        if path.is_symlink() or not path.is_file():
            return None
        observed = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None
    if (observed - now).total_seconds() > 5:
        return None
    return ActivityObservation(observed, source)


def _write_lease(clone: Path, state: LeaseState) -> None:
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
    atomic_write_text(lease_path(clone), payload)


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LeaseError(f"invalid lease timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LeaseError(f"{label} must be a positive integer")
    return value


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LeaseError(f"{label} must be nonempty")
    return value


def _append_runner_message(stream: Any, message: str) -> None:
    stream.write(f"\n[grok-worker] {message}\n".encode())
    stream.flush()
