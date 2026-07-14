"""Stable prompt prefix, content-addressed context packs, and honest metrics."""

from __future__ import annotations

import json
from pathlib import Path


def _manifest(task_id: str, outcome: str) -> dict[str, object]:
    return {
        "taskId": task_id,
        "outcome": outcome,
        "verification": ["pytest -q"],
        "constraints": ["grok-4.5/high", "no Fast"],
        "boundaries": {"allowedWrites": ["src"], "forbiddenWrites": ["secrets"]},
        "iterationPolicy": "one focused change",
        "stopWhen": "tests pass",
        "pauseIf": "user decision required",
    }


def test_context_pack_is_content_addressed_and_reused(tmp_path: Path) -> None:
    from grok_worker.prompt_cache import build_context_pack

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("stable\n", encoding="utf-8")
    cache = tmp_path / "cache"
    first = build_context_pack(repo, "abc123", cache)
    second = build_context_pack(repo, "abc123", cache)
    assert first.context_pack_hash == second.context_pack_hash
    assert first.path == second.path
    payload = json.loads(first.path.read_text(encoding="utf-8"))
    assert payload["baseSha"] == "abc123"
    assert payload["files"][0]["path"] == "README.md"
    assert str(repo) not in first.path.read_text(encoding="utf-8")


def test_dynamic_manifest_does_not_change_stable_prefix_hash(tmp_path: Path) -> None:
    from grok_worker.prompt_cache import Role, TaskManifest, build_context_pack, build_prompt

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("stable\n", encoding="utf-8")
    pack = build_context_pack(repo, "base", tmp_path / "cache")
    skill = Path(__file__).resolve().parents[1]
    first = build_prompt(skill, Role.IMPLEMENT, pack, TaskManifest.from_dict(_manifest("t", "a")))
    second = build_prompt(skill, Role.IMPLEMENT, pack, TaskManifest.from_dict(_manifest("t", "b")))
    assert first.stable_prefix_hash == second.stable_prefix_hash
    assert first.full_prompt != second.full_prompt
    assert first.stable_prefix not in second.followup_prompt
    assert '"outcome": "b"' in second.followup_prompt


def test_token_metrics_support_camel_snake_and_unobservable() -> None:
    from grok_worker.metrics import extract_token_metrics, extract_token_metrics_from_text

    camel = extract_token_metrics({"inputTokens": 100, "cachedReadTokens": 80, "outputTokens": 7})
    snake = extract_token_metrics({"input_tokens": 90, "cached_tokens": 45, "output_tokens": 5})
    missing = extract_token_metrics({"inputTokens": 10})
    assert camel.observable and camel.cache_ratio == 0.8
    assert snake.observable and snake.cache_ratio == 0.5
    assert not missing.observable
    assert missing.cached_tokens is None
    lines = "\n".join(
        [
            json.dumps({"event": "progress"}),
            json.dumps(
                {
                    "result": {
                        "_meta": {
                            "inputTokens": 200,
                            "cachedReadTokens": 150,
                            "outputTokens": 8,
                        }
                    }
                }
            ),
        ]
    )
    parsed = extract_token_metrics_from_text(lines)
    assert parsed.observable and parsed.cache_ratio == 0.75
