from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from grok_worker.cli import app
from grok_worker.root_registry import known_disposable_roots, register_disposable_root


def test_registry_deduplicates_roots(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    register_disposable_root(shared, first)
    register_disposable_root(shared, second)
    register_disposable_root(shared, first)

    assert known_disposable_roots(shared) == sorted(
        [first.resolve(), second.resolve()], key=lambda path: str(path).casefold()
    )


def test_default_health_aggregates_registered_roots(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    register_disposable_root(shared, first)
    register_disposable_root(shared, second)

    result = CliRunner().invoke(
        app,
        ["health", "--shared-cache-root", str(shared), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    roots = {str(Path(value).resolve()) for value in payload["roots"]}
    assert str(first.resolve()) in roots
    assert str(second.resolve()) in roots


def test_explicit_health_root_does_not_expand_registry(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    registered = tmp_path / "registered"
    explicit = tmp_path / "explicit"
    registered.mkdir()
    register_disposable_root(shared, registered)

    result = CliRunner().invoke(
        app,
        [
            "health",
            "--shared-cache-root",
            str(shared),
            "--disposable-root",
            str(explicit),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["roots"] == [str(explicit.resolve())]
