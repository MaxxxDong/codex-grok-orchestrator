"""Token/cache metric parsing with honest observability semantics."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class TokenMetrics:
    input_tokens: int | None
    cached_tokens: int | None
    output_tokens: int | None
    observable: bool

    @property
    def cache_ratio(self) -> float | None:
        if not self.observable or not self.input_tokens:
            return None
        return self.cached_tokens / self.input_tokens if self.cached_tokens is not None else None


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


def extract_token_metrics(payload: object) -> TokenMetrics:
    for mapping in _walk(payload):
        input_tokens = _integer(mapping, ("inputTokens", "input_tokens"))
        output_tokens = _integer(mapping, ("outputTokens", "output_tokens"))
        cached_tokens = _integer(
            mapping,
            ("cachedReadTokens", "cachedTokens", "cached_tokens", "cached_read_tokens"),
        )
        if input_tokens is not None or output_tokens is not None or cached_tokens is not None:
            return TokenMetrics(
                input_tokens=input_tokens,
                cached_tokens=cached_tokens,
                output_tokens=output_tokens,
                observable=input_tokens is not None and cached_tokens is not None,
            )
    return TokenMetrics(None, None, None, False)


def extract_token_metrics_from_text(text: str) -> TokenMetrics:
    """Find the latest token-bearing JSON object in an ACP JSON-lines log."""
    for line in reversed(text.splitlines()):
        try:
            payload: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        metrics = extract_token_metrics(payload)
        if any(
            value is not None
            for value in (metrics.input_tokens, metrics.cached_tokens, metrics.output_tokens)
        ):
            return metrics
    return TokenMetrics(None, None, None, False)


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
