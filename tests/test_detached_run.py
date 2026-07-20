"""Detached one-shot runs return promptly and are observed through events."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from grok_worker.cli import main


def _start_args(
    *,
    source: Path,
    roots: dict[str, Path],
    acpx_bin: Path,
    task_id: str,
    run_id: str,
    dispatcher_id: str,
) -> list[str]:
    return [
        "run",
        "--detach",
        "--source",
        str(source),
        "--prompt",
        "run the local fake backend",
        "--backend",
        "acp",
        "--acpx-bin",
        str(acpx_bin),
        "--task-id",
        task_id,
        "--run-id",
        run_id,
        "--dispatcher-id",
        dispatcher_id,
        "--disposable-root",
        str(roots["disposable"]),
        "--artifact-root",
        str(roots["artifacts"]),
        "--shared-cache-root",
        str(roots["shared"]),
        "--no-prepare-deps",
    ]


def _watch(
    *,
    roots: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    run_id: str | None = None,
    dispatcher_id: str | None = None,
    after: str = "",
    wait_seconds: float = 5,
) -> dict[str, object]:
    argv = [
        "watch",
        "--shared-cache-root",
        str(roots["shared"]),
        "--disposable-root",
        str(roots["disposable"]),
        "--after",
        after,
        "--wait-seconds",
        str(wait_seconds),
        "--json",
    ]
    if run_id:
        argv.extend(["--run-id", run_id])
    if dispatcher_id:
        argv.extend(["--dispatcher-id", dispatcher_id])
    assert main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_detached_run_returns_before_worker_and_watch_gets_terminal(
    git_source: Path,
    tmp_roots: dict[str, Path],
    fake_acpx_success: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FAKE_ACPX_DELAY_SECONDS", "0.8")
    started = time.monotonic()
    code = main(
        _start_args(
            source=git_source,
            roots=tmp_roots,
            acpx_bin=fake_acpx_success,
            task_id="detached-success",
            run_id="detached-run-success",
            dispatcher_id="detached-dispatcher",
        )
    )
    elapsed = time.monotonic() - started

    assert code == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["accepted"] is True
    assert receipt["run_id"] == "detached-run-success"
    assert receipt["dispatcher_id"] == "detached-dispatcher"
    assert isinstance(receipt["pid"], int)
    assert Path(receipt["launch_log"]).is_file()
    assert elapsed < 0.6

    payload = _watch(
        roots=tmp_roots,
        capsys=capsys,
        run_id="detached-run-success",
    )
    assert payload["kind"] == "events"
    events = payload["events"]
    assert isinstance(events, list)
    assert any(event["state"] == "success" for event in events)


def test_detached_startup_failure_wakes_watch_with_attention(
    tmp_roots: dict[str, Path],
    fake_acpx_success: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_roots["root"] / "missing-source"
    assert main(
        _start_args(
            source=missing,
            roots=tmp_roots,
            acpx_bin=fake_acpx_success,
            task_id="detached-missing",
            run_id="detached-run-missing",
            dispatcher_id="detached-dispatcher",
        )
    ) == 0
    capsys.readouterr()

    payload = _watch(
        roots=tmp_roots,
        capsys=capsys,
        run_id="detached-run-missing",
    )
    assert payload["kind"] == "events"
    assert payload["attention_required"] is True
    assert any(event["kind"] == "attention" for event in payload["events"])


def test_dispatcher_watch_observes_parallel_detached_wave(
    git_source: Path,
    tmp_roots: dict[str, Path],
    fake_acpx_success: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatcher_id = "detached-parallel-dispatcher"
    run_ids = {"parallel-detached-a", "parallel-detached-b", "parallel-detached-c"}
    for index, run_id in enumerate(sorted(run_ids)):
        monkeypatch.setenv("FAKE_ACPX_DELAY_SECONDS", str(0.1 + index * 0.1))
        monkeypatch.setenv("FAKE_ACPX_BEHAVIOR", "failure" if index == 1 else "success")
        assert main(
            _start_args(
                source=git_source,
                roots=tmp_roots,
                acpx_bin=fake_acpx_success,
                task_id=run_id,
                run_id=run_id,
                dispatcher_id=dispatcher_id,
            )
        ) == 0
        capsys.readouterr()

    terminal_runs: set[str] = set()
    cursor = ""
    deadline = time.monotonic() + 10
    while terminal_runs != run_ids and time.monotonic() < deadline:
        payload = _watch(
            roots=tmp_roots,
            capsys=capsys,
            dispatcher_id=dispatcher_id,
            after=cursor,
            wait_seconds=2,
        )
        cursor = str(payload["next_cursor"])
        for event in payload.get("events", []):
            if event.get("kind") == "terminal":
                terminal_runs.add(str(event["run_id"]))

    assert terminal_runs == run_ids
    assert os.path.isdir(tmp_roots["artifacts"])
