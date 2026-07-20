from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from grok_worker.activity_lease import (
    LEASE_TIMEOUT_EXIT_CODE,
    ActivityProbe,
    LeaseError,
    initialize_lease,
    lease_summary,
    read_lease,
    run_with_activity_lease,
    set_lease_policy,
)
from grok_worker.cli import main
from grok_worker.constants import MANAGED_BY, SCHEMA_VERSION
from grok_worker.models import WorkerMeta, WorkerState, dt_to_iso, utc_now
from grok_worker.paths import meta_dir, meta_path


def _clone(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()
    meta_dir(clone).mkdir()
    return clone


def _python_sleep(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_activity_probe_uses_native_grok_home(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    home = tmp_path / "home"
    probe = ActivityProbe(
        clone,
        environ={"HOME": str(home)},
    )

    assert probe.session_root.is_relative_to(home / ".grok" / "sessions")


def test_policy_can_change_while_lease_is_live(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    original = initialize_lease(clone, idle_timeout_seconds=30, hard_timeout_seconds=120)
    updated = set_lease_policy(clone, idle_timeout_seconds=60, hard_timeout_seconds=None)

    assert updated.idle_timeout_seconds == 60
    assert updated.hard_timeout_seconds is None
    assert updated.started_at == original.started_at
    assert updated.last_activity_at == original.last_activity_at
    assert updated.revision == original.revision + 1


def test_idle_lease_terminates_quiet_process(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    result = run_with_activity_lease(
        _python_sleep(3),
        clone=clone,
        log=tmp_path / "agent.log",
        env={},
        idle_timeout_seconds=1,
        hard_timeout_seconds=10,
        poll_seconds=0.02,
    )

    assert result.exit_code == LEASE_TIMEOUT_EXIT_CODE
    assert result.timeout_kind == "idle"
    assert "activity lease expired" in (result.timeout_message or "")


def test_quiet_process_still_runs_periodic_progress_tick(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    ticks = 0

    def on_tick() -> None:
        nonlocal ticks
        ticks += 1

    result = run_with_activity_lease(
        _python_sleep(0.2),
        clone=clone,
        log=tmp_path / "agent.log",
        env={},
        idle_timeout_seconds=2,
        hard_timeout_seconds=3,
        poll_seconds=0.02,
        on_tick=on_tick,
    )

    assert result.exit_code == 0
    assert ticks >= 2


def test_progress_updates_renew_idle_lease(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    progress = meta_dir(clone) / "progress.json"

    def update_progress() -> None:
        for index in range(6):
            time.sleep(0.25)
            progress.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "step": "editing",
                        "updated_at": str(index),
                    }
                ),
                encoding="utf-8",
            )

    updater = threading.Thread(target=update_progress)
    updater.start()
    result = run_with_activity_lease(
        _python_sleep(1.4),
        clone=clone,
        log=tmp_path / "agent.log",
        env={},
        idle_timeout_seconds=1,
        hard_timeout_seconds=10,
        poll_seconds=0.02,
    )
    updater.join()

    assert result.exit_code == 0
    assert read_lease(clone).last_activity_source == "progress"


def test_live_policy_extension_prevents_idle_timeout(tmp_path: Path) -> None:
    clone = _clone(tmp_path)

    def extend() -> None:
        while not (meta_dir(clone) / "lease.json").exists():
            time.sleep(0.01)
        time.sleep(0.4)
        set_lease_policy(clone, idle_timeout_seconds=3)

    extender = threading.Thread(target=extend)
    extender.start()
    result = run_with_activity_lease(
        _python_sleep(1.5),
        clone=clone,
        log=tmp_path / "agent.log",
        env={},
        idle_timeout_seconds=1,
        hard_timeout_seconds=10,
        poll_seconds=0.02,
    )
    extender.join()

    assert result.exit_code == 0
    assert read_lease(clone).idle_timeout_seconds == 3


def test_hard_limit_remains_a_separate_safety_cap(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    result = run_with_activity_lease(
        _python_sleep(3),
        clone=clone,
        log=tmp_path / "agent.log",
        env={},
        idle_timeout_seconds=10,
        hard_timeout_seconds=1,
        poll_seconds=0.02,
    )

    assert result.exit_code == LEASE_TIMEOUT_EXIT_CODE
    assert result.timeout_kind == "hard"


def test_lease_summary_reports_dynamic_remaining_time(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    initialize_lease(clone, idle_timeout_seconds=60, hard_timeout_seconds=120)

    summary = lease_summary(clone)

    assert summary is not None
    assert summary["timeout_mode"] == "activity_lease"
    assert summary["idle_timeout_seconds"] == 60
    assert 0 < float(summary["lease_remaining_seconds"]) <= 60
    assert 0 < float(summary["hard_remaining_seconds"]) <= 120


def test_lease_set_cli_adjusts_live_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    disposable = tmp_path / "disposable"
    clone = disposable / "grok-worker-live-task"
    clone.mkdir(parents=True)
    meta_dir(clone).mkdir()
    now = dt_to_iso(utc_now()) or ""
    WorkerMeta(
        schema_version=SCHEMA_VERSION,
        task_id="live-task",
        source_realpath=str(tmp_path),
        clone_realpath=str(clone.resolve()),
        state=WorkerState.RUNNING,
        created_at=now,
        updated_at=now,
        managed_by=MANAGED_BY,
    ).write(meta_path(clone))
    initialize_lease(clone, idle_timeout_seconds=30, hard_timeout_seconds=120)

    code = main(
        [
            "lease-set",
            "--disposable-root",
            str(disposable),
            "--task-id",
            "live-task",
            "--idle-timeout",
            "90",
            "--hard-timeout",
            "0",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["idle_timeout_seconds"] == 90
    assert payload["hard_timeout_seconds"] is None
    assert payload["revision"] == 2


def test_lease_reader_refuses_symlink_leaf(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    path = meta_dir(clone) / "lease.json"
    path.symlink_to(target)

    with pytest.raises(LeaseError, match="symlink"):
        read_lease(clone)


def test_lease_lock_refuses_symlink_leaf(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    target = tmp_path / "outside.lock"
    target.write_text("do not touch", encoding="utf-8")
    (meta_dir(clone) / "lease.lock").symlink_to(target)

    with pytest.raises(RuntimeError, match="symlink"):
        initialize_lease(clone, idle_timeout_seconds=30, hard_timeout_seconds=60)
    assert target.read_text(encoding="utf-8") == "do not touch"
