"""Token/cache metric parsing with honest observability semantics."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

# Machine-readable bases for cache_ratio. Grok reports fresh input and
# cache-read input separately; OpenAI nested details count cached inside total.
_BASIS_GROK_SEPARATE = "cached_over_fresh_plus_cached"
_BASIS_OPENAI_TOTAL = "cached_over_total_input"
_BASIS_LEGACY = "legacy_cached_over_input"

_GROK_CACHE_KEYS = ("cache_read_input_tokens", "cacheReadInputTokens")
_LEGACY_CACHE_KEYS = (
    "cachedReadTokens",
    "cachedTokens",
    "cached_tokens",
    "cached_read_tokens",
)
_MODEL_CALL_KEYS = ("num_turns", "modelCalls", "model_calls")


@dataclass(frozen=True)
class TokenMetrics:
    input_tokens: int | None
    cached_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    observable: bool
    model_calls: int | None = None
    input_includes_cached: bool | None = None
    cache_ratio_basis: str | None = None

    @property
    def cache_ratio(self) -> float | None:
        if not self.observable or self.cached_tokens is None:
            return None
        if self.input_tokens is None or self.input_tokens < 0 or self.cached_tokens < 0:
            return None
        if self.cache_ratio_basis == _BASIS_GROK_SEPARATE:
            denominator = self.input_tokens + self.cached_tokens
            if denominator <= 0:
                return None
            return self.cached_tokens / denominator
        if not self.input_tokens:
            return None
        if self.cached_tokens > self.input_tokens:
            return None
        return self.cached_tokens / self.input_tokens


def _walk(value: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        current = {str(key): item for key, item in value.items()}
        found.append(current)
        for item in current.values():
            found.extend(_walk(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk(item))
    return found


def _integer(mapping: dict[str, object], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _extract_model_calls(payload: object) -> int | None:
    """Return the first authoritative model-call count; never sum nested copies."""
    for mapping in _walk(payload):
        value = _integer(mapping, _MODEL_CALL_KEYS)
        if value is not None and value >= 0:
            return value
    return None


def _classify_cache(
    mapping: dict[str, object],
) -> tuple[int | None, bool | None, str | None]:
    grok_cached = _integer(mapping, _GROK_CACHE_KEYS)
    if grok_cached is not None:
        return grok_cached, False, _BASIS_GROK_SEPARATE

    input_details = mapping.get("input_tokens_details")
    if isinstance(input_details, dict):
        openai_cached = _integer(input_details, ("cached_tokens",))
        if openai_cached is not None:
            return openai_cached, True, _BASIS_OPENAI_TOTAL

    legacy_cached = _integer(mapping, _LEGACY_CACHE_KEYS)
    if legacy_cached is not None:
        return legacy_cached, True, _BASIS_LEGACY

    return None, None, None


def extract_token_metrics(payload: object) -> TokenMetrics:
    model_calls = _extract_model_calls(payload)
    for mapping in _walk(payload):
        input_tokens = _integer(mapping, ("inputTokens", "input_tokens"))
        output_tokens = _integer(mapping, ("outputTokens", "output_tokens"))
        reasoning_tokens = _integer(mapping, ("reasoningTokens", "reasoning_tokens"))
        cached_tokens, input_includes_cached, basis = _classify_cache(mapping)
        output_details = mapping.get("output_tokens_details")
        if reasoning_tokens is None and isinstance(output_details, dict):
            reasoning_tokens = _integer(output_details, ("reasoning_tokens",))
        if any(
            item is not None
            for item in (input_tokens, output_tokens, cached_tokens, reasoning_tokens)
        ):
            return TokenMetrics(
                input_tokens=input_tokens,
                cached_tokens=cached_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                observable=input_tokens is not None and cached_tokens is not None,
                model_calls=model_calls,
                input_includes_cached=input_includes_cached,
                cache_ratio_basis=basis if cached_tokens is not None else None,
            )
    return TokenMetrics(
        None,
        None,
        None,
        None,
        False,
        model_calls=model_calls,
    )


def _metric_values(metrics: TokenMetrics) -> tuple[int | None, ...]:
    return (
        metrics.input_tokens,
        metrics.cached_tokens,
        metrics.output_tokens,
        metrics.reasoning_tokens,
        metrics.model_calls,
    )


def _embedded_json_metrics(text: str) -> TokenMetrics:
    """Extract the richest complete JSON object embedded after log prefixes."""
    decoder = json.JSONDecoder()
    best = TokenMetrics(None, None, None, None, False)
    best_score = 0
    for index in (pos for pos in range(len(text) - 1, -1, -1) if text[pos] == "{"):
        try:
            payload, _end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        metrics = extract_token_metrics(payload)
        score = sum(value is not None for value in _metric_values(metrics))
        if score > best_score:
            best = metrics
            best_score = score
            if score >= 5:
                break
    return best


def extract_token_metrics_from_text(text: str) -> TokenMetrics:
    """Find the latest token-bearing JSON object in an ACP JSON-lines log."""
    try:
        whole_payload: object = json.loads(text)
    except json.JSONDecodeError:
        whole_payload = None
    if whole_payload is not None:
        whole_metrics = extract_token_metrics(whole_payload)
        if any(value is not None for value in _metric_values(whole_metrics)):
            return whole_metrics
        if isinstance(whole_payload, dict):
            nested = whole_payload.get("agent_output")
            if isinstance(nested, str) and nested != text:
                nested_metrics = extract_token_metrics_from_text(nested)
                if any(value is not None for value in _metric_values(nested_metrics)):
                    return nested_metrics
    for line in reversed(text.splitlines()):
        try:
            payload: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        metrics = extract_token_metrics(payload)
        if any(value is not None for value in _metric_values(metrics)):
            return metrics
        if isinstance(payload, dict):
            # Finalized worker logs wrap native Grok's JSON output in the
            # ``agent_output`` string. Parse that nested JSON-lines stream too.
            nested = payload.get("agent_output")
            if isinstance(nested, str) and nested != text:
                nested_metrics = extract_token_metrics_from_text(nested)
                if any(value is not None for value in _metric_values(nested_metrics)):
                    return nested_metrics
    return _embedded_json_metrics(text)


def append_metric(path: Path, record: dict[str, object], metrics: TokenMetrics) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**record, **asdict(metrics), "cache_ratio": metrics.cache_ratio}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def read_task_metrics(path: Path, task_id: str) -> list[dict[str, object]]:
    if not path.is_file() or path.is_symlink():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("task_id") == task_id:
            records.append({str(key): value for key, value in payload.items()})
    return records
