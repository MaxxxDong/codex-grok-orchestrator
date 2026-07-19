"""One-pass disclosure preflight reports every blocked path without values."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grok_worker.cli import main
from grok_worker.disclosure import disclosure_preflight


def test_preflight_reports_all_blocked_dirty_paths_once(
    git_source: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tests = git_source / "tests"
    tests.mkdir()
    first_value = "ghp_" + "FAKEDETERMINISTICVALUE123456"
    second_value = "sk-test-" + "ANOTHERDETERMINISTICVALUE123456"
    (tests / "github-token.test.ts").write_text(
        f'const API_KEY = "{first_value}";\n', encoding="utf-8"
    )
    (tests / "bearer-token.test.ts").write_text(
        f'const token = "{second_value}";\n', encoding="utf-8"
    )

    code = main(["preflight", "--source", str(git_source), "--json"])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["allowed"] is False
    assert payload["blocked_count"] == 2
    assert payload["values_exposed"] is False
    assert {item["path"] for item in payload["blocked"]} == {
        "tests/bearer-token.test.ts",
        "tests/github-token.test.ts",
    }
    assert {item["reason_code"] for item in payload["blocked"]} == {
        "credential_shaped_token"
    }
    serialized = json.dumps(payload)
    assert first_value not in serialized
    assert second_value not in serialized


def test_runtime_composed_test_values_pass_preflight(git_source: Path) -> None:
    tests = git_source / "tests"
    tests.mkdir()
    (tests / "synthetic-token.test.ts").write_text(
        'const API_KEY = "ghp_" + "FAKEDETERMINISTICVALUE123456";\n'
        'const token = "sk-test-" + "ANOTHERDETERMINISTICVALUE123456";\n',
        encoding="utf-8",
    )

    payload = disclosure_preflight(git_source)
    assert payload["allowed"] is True
    assert payload["blocked_count"] == 0
    assert payload["included_dirty_count"] == 1


def test_token_named_identifier_assignments_are_not_credential_literals(
    git_source: Path,
) -> None:
    (git_source / "runtime.py").write_text(
        "start_token = process_start_token(child.pid)\n"
        "pid, token = capture_identity()\n"
        'runner_start_token = "not-" + "a-real-token"\n',
        encoding="utf-8",
    )

    payload = disclosure_preflight(git_source)
    assert payload["allowed"] is True
    assert payload["blocked_count"] == 0


def test_unquoted_credential_shaped_value_still_fails_closed(git_source: Path) -> None:
    (git_source / "unsafe.env.txt").write_text(
        "API_KEY=" + "sk_test_" + "1234567890abcdef1234567890\n",
        encoding="utf-8",
    )

    payload = disclosure_preflight(git_source)
    assert payload["allowed"] is False
    assert payload["blocked"] == [
        {"path": "unsafe.env.txt", "reason_code": "credential_shaped_token"}
    ]


def test_run_refusal_prints_every_blocked_path_without_values(
    git_source: Path,
    tmp_roots: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_value = "ghp_" + "FAKEDETERMINISTICVALUE123456"
    second_value = "sk-test-" + "ANOTHERDETERMINISTICVALUE123456"
    (git_source / "first.test.ts").write_text(
        f'const API_KEY = "{first_value}";\n', encoding="utf-8"
    )
    (git_source / "second.test.ts").write_text(
        f'const token = "{second_value}";\n', encoding="utf-8"
    )

    code = main(
        [
            "run",
            "--source",
            str(git_source),
            "--prompt",
            "must stop before launch",
            "--task-id",
            "disclosure-all-paths",
            "--run-id",
            "disclosure-run-1",
            "--disposable-root",
            str(tmp_roots["disposable"]),
            "--artifact-root",
            str(tmp_roots["artifacts"]),
            "--shared-cache-root",
            str(tmp_roots["shared"]),
            "--no-prepare-deps",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "first.test.ts reason=credential_shaped_token" in captured.err
    assert "second.test.ts reason=credential_shaped_token" in captured.err
    assert first_value not in captured.err
    assert second_value not in captured.err
