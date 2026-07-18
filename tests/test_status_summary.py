"""Status summary seam (per-clone phase/activity/timeout/resources).

Public contract:
- ``grok-worker status --json`` each clone includes a summary derived from
  lifecycle/progress/file times and active PID:
  phase, last_activity_at, elapsed_seconds, timeout_seconds, remaining_seconds,
  timeout_mode, hard_timeout_seconds, hard_remaining_seconds, lease_revision,
  result_ready, artifact_ready, resources(cpu_percent/rss_bytes; null when
  unsupported).
- progress is advisory only; lifecycle state remains authoritative.
- illegal/stale progress must fail soft and never fake success.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from grok_worker.cli import main
from grok_worker.constants import MANAGED_BY, SCHEMA_VERSION
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import meta_dir, meta_path
from grok_worker.process_identity import process_start_token
from grok_worker.status import collect_status

REQUIRED_SUMMARY_KEYS = (
    "phase",
    "last_activity_at",
    "activity_source",
    "progress_step",
    "elapsed_seconds",
    "timeout_seconds",
    "remaining_seconds",
    "timeout_mode",
    "hard_timeout_seconds",
    "hard_remaining_seconds",
    "lease_revision",
    "result_ready",
    "artifact_ready",
    "resources",
)


def _write_running_clone(
    disposable: Path,
    name: str,
    *,
    pid: int | None = None,
    token: str | None = None,
    created_offset_seconds: int = 30,
) -> Path:
    clone = disposable / name
    clone.mkdir(parents=True)
    (clone / "work.txt").write_text("in progress\n", encoding="utf-8")
    now = utc_now()
    created = now - timedelta(seconds=created_offset_seconds)
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id=name,
        source_realpath="/tmp/src-status-summary",
        clone_realpath=str(clone.resolve()),
        state=WorkerState.RUNNING,
        created_at=dt_to_iso(created) or "",
        updated_at=dt_to_iso(now) or "",
        managed_by=MANAGED_BY,
        runner_pid=pid,
        runner_start_token=token,
        pid=pid,
    )
    meta_dir(clone).mkdir(parents=True, exist_ok=True)
    meta.write(meta_path(clone))
    return clone


def _assert_summary_fields(clone_entry: dict) -> None:
    missing = [k for k in REQUIRED_SUMMARY_KEYS if k not in clone_entry]
    assert not missing, (
        "status --json clone entry must include summary fields "
        f"{list(REQUIRED_SUMMARY_KEYS)}; missing={missing}; entry={clone_entry}"
    )
    assert isinstance(clone_entry["phase"], str) and clone_entry["phase"]
    assert (
        isinstance(clone_entry["last_activity_at"], str)
        and clone_entry["last_activity_at"]
    )
    assert clone_entry["activity_source"] in {
        "lifecycle",
        "progress",
        "workspace",
        "result",
        "grok_session",
        "process_start",
    }
    assert clone_entry["progress_step"] is None or clone_entry["progress_step"] in {
        "planning",
        "editing",
        "verifying",
        "finalizing",
    }
    assert isinstance(clone_entry["elapsed_seconds"], (int, float))
    assert clone_entry["elapsed_seconds"] >= 0
    # timeout/remaining may be null when unknown, but keys must exist
    assert "timeout_seconds" in clone_entry
    assert "remaining_seconds" in clone_entry
    assert isinstance(clone_entry["result_ready"], bool)
    assert isinstance(clone_entry["artifact_ready"], bool)
    resources = clone_entry["resources"]
    assert isinstance(resources, dict), "resources must be an object"
    assert "cpu_percent" in resources
    assert "rss_bytes" in resources
    # unsupported platforms may report null
    for key in ("cpu_percent", "rss_bytes"):
        val = resources[key]
        assert val is None or isinstance(val, (int, float)), (
            f"resources.{key} must be number or null, got {val!r}"
        )


def test_status_json_includes_per_clone_summary(
    tmp_roots: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy path: managed running clone exposes full summary block via status --json."""
    disposable = tmp_roots["disposable"]
    shared = tmp_roots["shared"]
    pid = os.getpid()
    token = process_start_token(pid)
    clone = _write_running_clone(
        disposable,
        "sum-run-01",
        pid=pid,
        token=token,
        created_offset_seconds=45,
    )
    # Advisory progress only (must not override lifecycle authority).
    progress = meta_dir(clone) / "progress.json"
    progress.write_text(
        json.dumps(
            {
                "phase": "running",
                "updated_at": dt_to_iso(utc_now()),
                "message": "working",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    code = main(
        [
            "status",
            "--disposable-root",
            str(disposable),
            "--shared-cache-root",
            str(shared),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    clones = payload.get("clones")
    assert isinstance(clones, list) and clones, f"expected managed clones, got {payload}"
    entry = next((c for c in clones if c.get("name") == "sum-run-01"), None)
    assert entry is not None, f"clone sum-run-01 missing from status: {clones}"
    _assert_summary_fields(entry)
    # Lifecycle is authoritative: phase must reflect lifecycle running state.
    assert entry["phase"] in {"running", "RUNNING", str(WorkerState.RUNNING)}
    assert entry["result_ready"] is False
    assert entry["artifact_ready"] is False
    assert entry["elapsed_seconds"] >= 1


def test_status_illegal_progress_fails_soft_and_stays_lifecycle_authoritative(
    tmp_roots: dict[str, Path],
) -> None:
    """Boundary: corrupt/stale progress must not crash or fake success."""
    disposable = tmp_roots["disposable"]
    shared = tmp_roots["shared"]
    clone = _write_running_clone(
        disposable,
        "sum-bad-prog",
        pid=None,
        token=None,
        created_offset_seconds=10,
    )
    # Illegal progress payloads (not JSON / wrong types / claims success).
    progress = meta_dir(clone) / "progress.json"
    progress.write_text(
        '{"phase": "success", "updated_at": "not-a-timestamp", "bogus": true\n',
        encoding="utf-8",
    )

    report = collect_status(disposable, shared_cache_root=shared)
    assert len(report.clones) == 1
    entry = report.to_dict()["clones"][0]
    assert isinstance(entry, dict)
    _assert_summary_fields(entry)
    # Must not promote illegal progress "success" over lifecycle running.
    assert entry["state"] == "running"
    assert entry["phase"] != "success"
    assert entry.get("result_ready") is False
    # Fail-soft: status collection itself must not raise (already returned).


def test_status_timeout_null_when_legacy_metadata(
    tmp_roots: dict[str, Path],
) -> None:
    """Backward compatible: missing timeout_seconds on lifecycle yields null."""
    disposable = tmp_roots["disposable"]
    shared = tmp_roots["shared"]
    clone = _write_running_clone(
        disposable,
        "sum-legacy-to",
        pid=None,
        token=None,
        created_offset_seconds=5,
    )
    path = meta_path(clone)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("timeout_seconds", None)
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")

    report = collect_status(disposable, shared_cache_root=shared)
    entry = report.to_dict()["clones"][0]
    assert isinstance(entry, dict)
    _assert_summary_fields(entry)
    assert entry["timeout_seconds"] is None
    assert entry["remaining_seconds"] is None


def test_status_inactive_resources_null(
    tmp_roots: dict[str, Path],
) -> None:
    """Resources are null when the process is not active."""
    disposable = tmp_roots["disposable"]
    shared = tmp_roots["shared"]
    _write_running_clone(
        disposable,
        "sum-dead-res",
        pid=999_999_992,
        token="dead-token",
        created_offset_seconds=12,
    )
    report = collect_status(disposable, shared_cache_root=shared)
    entry = report.to_dict()["clones"][0]
    assert isinstance(entry, dict)
    _assert_summary_fields(entry)
    assert entry["active"] is False
    assert entry["resources"]["cpu_percent"] is None
    assert entry["resources"]["rss_bytes"] is None


def test_future_progress_timestamp_does_not_override_lifecycle(
    tmp_roots: dict[str, Path],
) -> None:
    """Future progress timestamps beyond clock-skew tolerance must not win activity."""
    from datetime import UTC, datetime

    from grok_worker.status import CLOCK_SKEW_TOLERANCE, build_clone_summary

    disposable = tmp_roots["disposable"]
    clone = _write_running_clone(
        disposable,
        "sum-future-ts",
        pid=None,
        token=None,
        created_offset_seconds=60,
    )
    meta = WorkerMeta.read(meta_path(clone))
    lifecycle_updated = meta.updated_at
    # Far-future progress (well beyond CLOCK_SKEW_TOLERANCE).
    far_future = (utc_now() + timedelta(hours=2)).isoformat()
    progress = meta_dir(clone) / "progress.json"
    progress.write_text(
        json.dumps({"phase": "running", "updated_at": far_future}) + "\n",
        encoding="utf-8",
    )
    now = utc_now()
    summary = build_clone_summary(meta, clone, now=now, active=False)
    activity = summary["last_activity_at"]
    # Must not adopt the far-future progress timestamp.
    assert far_future not in activity or activity == lifecycle_updated
    act_dt = datetime.fromisoformat(str(activity))
    if act_dt.tzinfo is None:
        act_dt = act_dt.replace(tzinfo=UTC)
    assert act_dt <= now + CLOCK_SKEW_TOLERANCE
    # Lifecycle updated_at should still be the preferred usable baseline.
    assert activity == lifecycle_updated or act_dt <= now + CLOCK_SKEW_TOLERANCE


def test_workspace_write_advances_activity_without_progress(
    tmp_roots: dict[str, Path],
) -> None:
    """Real source edits must be visible even when the model emits no progress file."""
    from datetime import UTC, datetime

    from grok_worker.status import build_clone_summary

    disposable = tmp_roots["disposable"]
    clone = _write_running_clone(
        disposable,
        "sum-workspace-activity",
        pid=None,
        token=None,
        created_offset_seconds=120,
    )
    meta = WorkerMeta.read(meta_path(clone))
    old = utc_now() - timedelta(seconds=90)
    meta.updated_at = dt_to_iso(old) or ""
    meta.write(meta_path(clone))

    source = clone / "src" / "feature.py"
    source.parent.mkdir()
    source.write_text("print('working')\n", encoding="utf-8")
    now = utc_now()
    summary = build_clone_summary(meta, clone, now=now, active=False)

    activity = datetime.fromisoformat(str(summary["last_activity_at"]))
    if activity.tzinfo is None:
        activity = activity.replace(tzinfo=UTC)
    assert activity > old
    assert summary["activity_source"] == "workspace"


def test_workspace_activity_refuses_symlink_escape(
    tmp_roots: dict[str, Path],
) -> None:
    """An outside file reached through a symlink must never manufacture activity."""
    from grok_worker.status import build_clone_summary

    disposable = tmp_roots["disposable"]
    clone = _write_running_clone(
        disposable,
        "sum-workspace-symlink",
        pid=None,
        token=None,
        created_offset_seconds=120,
    )
    meta = WorkerMeta.read(meta_path(clone))
    outside = tmp_roots["shared"] / "outside.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("outside\n", encoding="utf-8")
    link = clone / "outside-link"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    summary = build_clone_summary(meta, clone, now=utc_now(), active=False)
    assert summary["activity_source"] != "workspace"


def test_progress_step_is_bounded_allowlist_only(
    tmp_roots: dict[str, Path],
) -> None:
    """Expose a safe fixed phase hint, never arbitrary model-authored text."""
    from grok_worker.status import build_clone_summary

    disposable = tmp_roots["disposable"]
    clone = _write_running_clone(disposable, "sum-progress-step")
    meta = WorkerMeta.read(meta_path(clone))
    progress = meta_dir(clone) / "progress.json"
    progress.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "step": "verifying",
                "updated_at": dt_to_iso(utc_now()),
                "message": "TOKEN_DO_NOT_SURFACE",
            }
        ),
        encoding="utf-8",
    )
    summary = build_clone_summary(meta, clone, active=False)
    assert summary["progress_step"] == "verifying"
    assert summary["activity_source"] == "progress"
    assert "TOKEN_DO_NOT_SURFACE" not in json.dumps(summary)

    progress.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "step": "success",
                "updated_at": dt_to_iso(utc_now()),
            }
        ),
        encoding="utf-8",
    )
    invalid = build_clone_summary(meta, clone, active=False)
    assert invalid["progress_step"] is None


def test_terminal_elapsed_frozen_remaining_null(
    tmp_roots: dict[str, Path],
) -> None:
    """Terminal success/failed/keep freeze elapsed at updated_at - created_at."""
    from datetime import UTC, datetime

    from grok_worker.status import build_clone_summary

    disposable = tmp_roots["disposable"]
    clone = disposable / "sum-terminal-freeze"
    clone.mkdir(parents=True)
    created = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    updated = datetime(2026, 1, 1, 12, 5, 0, tzinfo=UTC)  # 300s later
    meta = WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="sum-terminal-freeze",
        source_realpath="/tmp/src-status-summary",
        clone_realpath=str(clone.resolve()),
        state=WorkerState.SUCCESS,
        created_at=dt_to_iso(created) or "",
        updated_at=dt_to_iso(updated) or "",
        managed_by=MANAGED_BY,
        timeout_seconds=600,
        artifact_complete=True,
        artifact_path=str(clone / "art"),
    )
    meta_dir(clone).mkdir(parents=True, exist_ok=True)
    meta.write(meta_path(clone))

    # "Now" is long after terminal transition — elapsed must not grow.
    now = datetime(2026, 1, 1, 15, 0, 0, tzinfo=UTC)
    summary = build_clone_summary(meta, clone, now=now, active=False)
    assert summary["elapsed_seconds"] == pytest.approx(300.0)
    assert summary["remaining_seconds"] is None
    assert summary["timeout_seconds"] == 600

    # Second sample further in the future stays frozen.
    later = datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)
    summary2 = build_clone_summary(meta, clone, now=later, active=False)
    assert summary2["elapsed_seconds"] == pytest.approx(300.0)
    assert summary2["remaining_seconds"] is None


def test_resource_pid_prefers_acpx_then_runner_then_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resource sampling PID order: acpx_pid → runner_pid → legacy pid."""
    from grok_worker import status as status_mod
    from grok_worker.status import preferred_resource_pid

    sampled: list[int | None] = []

    def capture_pid(pid: int | None) -> dict[str, float | int | None]:
        sampled.append(pid)
        return {"cpu_percent": 1.0, "rss_bytes": 1024}

    monkeypatch.setattr(status_mod, "process_resources", capture_pid)

    base = dict(
        schema_version=SCHEMA_VERSION,
        task_id="pid-order",
        source_realpath="/tmp/s",
        clone_realpath="/tmp/c",
        state=WorkerState.RUNNING,
        created_at=dt_to_iso(utc_now()) or "",
        updated_at=dt_to_iso(utc_now()) or "",
        managed_by=MANAGED_BY,
    )
    # All three present → acpx wins.
    meta = WorkerMeta(**base, acpx_pid=111, runner_pid=222, pid=333)
    assert preferred_resource_pid(meta) == 111
    status_mod._resources_for(meta, True)
    assert sampled[-1] == 111

    # No acpx → runner.
    meta2 = WorkerMeta(**base, acpx_pid=None, runner_pid=222, pid=333)
    assert preferred_resource_pid(meta2) == 222
    status_mod._resources_for(meta2, True)
    assert sampled[-1] == 222

    # Only legacy pid.
    meta3 = WorkerMeta(**base, acpx_pid=None, runner_pid=None, pid=333)
    assert preferred_resource_pid(meta3) == 333
    status_mod._resources_for(meta3, True)
    assert sampled[-1] == 333
