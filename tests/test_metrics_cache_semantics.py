"""Regression tests for cache-ratio semantics, model_calls, and bounded ratio."""

from __future__ import annotations

import json

from grok_worker.metrics import extract_token_metrics, extract_token_metrics_from_text


def test_openai_nested_cached_tokens_ratio_is_cached_over_total() -> None:
    metrics = extract_token_metrics(
        {
            "input_tokens": 1000,
            "output_tokens": 50,
            "input_tokens_details": {"cached_tokens": 250},
        }
    )
    assert metrics.observable
    assert metrics.cached_tokens == 250
    assert metrics.input_includes_cached is True
    assert metrics.cache_ratio == 0.25
    assert metrics.cache_ratio_basis == "cached_over_total_input"


def test_grok_separate_cache_read_ratio_is_cached_over_fresh_plus_cached() -> None:
    # Real-world shape: fresh input + separate cache-read can exceed input alone.
    metrics = extract_token_metrics(
        {
            "usage": {
                "input_tokens": 167254,
                "cache_read_input_tokens": 1847296,
                "output_tokens": 1200,
                "reasoning_tokens": 400,
            }
        }
    )
    assert metrics.observable
    assert metrics.input_tokens == 167254
    assert metrics.cached_tokens == 1847296
    assert metrics.input_includes_cached is False
    expected = 1847296 / (167254 + 1847296)
    assert metrics.cache_ratio is not None
    assert abs(metrics.cache_ratio - expected) < 1e-12
    assert 0.0 <= metrics.cache_ratio <= 1.0
    assert metrics.cache_ratio_basis == "cached_over_fresh_plus_cached"


def test_grok_camel_cache_read_input_tokens_same_semantics() -> None:
    metrics = extract_token_metrics(
        {"inputTokens": 100, "cacheReadInputTokens": 300, "outputTokens": 1}
    )
    assert metrics.input_includes_cached is False
    assert metrics.cache_ratio == 300 / 400
    assert metrics.cache_ratio_basis == "cached_over_fresh_plus_cached"


def test_legacy_cached_tokens_ratio_remains_cached_over_input() -> None:
    snake = extract_token_metrics(
        {"input_tokens": 90, "cached_tokens": 45, "output_tokens": 5}
    )
    camel = extract_token_metrics(
        {"inputTokens": 100, "cachedReadTokens": 80, "outputTokens": 7}
    )
    assert snake.cache_ratio == 0.5
    assert snake.input_includes_cached is True
    assert snake.cache_ratio_basis == "legacy_cached_over_input"
    assert camel.cache_ratio == 0.8
    assert camel.cache_ratio_basis == "legacy_cached_over_input"


def test_incoherent_total_cache_fields_are_not_reported_as_a_ratio() -> None:
    metrics = extract_token_metrics(
        {
            "input_tokens": 10,
            "cached_tokens": 50,
            "output_tokens": 1,
        }
    )
    assert metrics.cache_ratio is None


def test_model_calls_from_num_turns_and_model_calls_without_nested_double_count() -> None:
    # Real 33-call Grok payload shape: outer num_turns plus nested duplicates.
    payload = {
        "text": "done",
        "num_turns": 33,
        "usage": {
            "input_tokens": 167254,
            "cache_read_input_tokens": 1847296,
            "output_tokens": 900,
            "reasoning_tokens": 120,
            "num_turns": 33,
            "modelCalls": 33,
        },
        "stats": {"modelCalls": 33},
    }
    metrics = extract_token_metrics(payload)
    assert metrics.model_calls == 33
    assert metrics.cache_ratio is not None
    assert metrics.cache_ratio <= 1.0

    camel_only = extract_token_metrics(
        {"inputTokens": 10, "cachedTokens": 2, "modelCalls": 4, "outputTokens": 1}
    )
    assert camel_only.model_calls == 4


def test_top_level_model_call_count_wins_over_conflicting_nested_copy() -> None:
    metrics = extract_token_metrics(
        {
            "num_turns": 7,
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 90,
                "modelCalls": 99,
            },
        }
    )
    assert metrics.model_calls == 7


def test_model_calls_parsed_from_native_text_log() -> None:
    log = json.dumps(
        {
            "agent_output": json.dumps(
                {
                    "text": "ok",
                    "num_turns": 33,
                    "usage": {
                        "input_tokens": 100,
                        "cache_read_input_tokens": 900,
                        "output_tokens": 20,
                        "reasoning_tokens": 5,
                    },
                }
            )
        }
    )
    metrics = extract_token_metrics_from_text(log)
    assert metrics.model_calls == 33
    assert metrics.input_includes_cached is False
    assert metrics.cache_ratio == 900 / 1000
