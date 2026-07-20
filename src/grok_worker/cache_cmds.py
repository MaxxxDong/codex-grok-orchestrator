"""Typer commands for the independent shared-cache capacity domain."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from grok_worker.cache_policy import (
    DEFAULT_CACHE_MAX_BYTES,
    DEFAULT_CACHE_TTL_HOURS,
    CachePolicy,
    cache_usage_bytes,
    default_cache_root,
    gc_shared_cache,
)


def _policy(root: Path | None, maximum: int, ttl: float) -> CachePolicy:
    return CachePolicy(
        root=(root.resolve() if root else default_cache_root()),
        max_bytes=maximum,
        ttl_hours=ttl,
    )


def cmd_cache_status(
    shared_cache_root: Path | None = typer.Option(None, "--shared-cache-root"),
    max_bytes: int = typer.Option(DEFAULT_CACHE_MAX_BYTES, "--max-bytes"),
    ttl_hours: float = typer.Option(DEFAULT_CACHE_TTL_HOURS, "--ttl-hours"),
    json_flag: bool = typer.Option(
        False,
        "--json",
        help="Compatibility flag; output is always one JSON document.",
    ),
) -> None:
    """Report shared-cache usage without modifying it."""
    del json_flag  # accepted for CLI compatibility; format is always JSON
    policy = _policy(shared_cache_root, max_bytes, ttl_hours)
    usage = cache_usage_bytes(policy.root)
    typer.echo(
        json.dumps(
            {
                "root": str(policy.root),
                "usage_bytes": usage,
                "max_bytes": policy.max_bytes,
                "ttl_hours": policy.ttl_hours,
                "over_limit": usage > policy.max_bytes,
            },
            indent=2,
        )
    )


def cmd_cache_gc(
    shared_cache_root: Path | None = typer.Option(None, "--shared-cache-root"),
    max_bytes: int = typer.Option(DEFAULT_CACHE_MAX_BYTES, "--max-bytes"),
    ttl_hours: float = typer.Option(DEFAULT_CACHE_TTL_HOURS, "--ttl-hours"),
    json_flag: bool = typer.Option(
        False,
        "--json",
        help="Compatibility flag; output is always one JSON document.",
    ),
) -> None:
    """Apply TTL then LRU eviction inside the shared-cache domain."""
    del json_flag  # accepted for CLI compatibility; format is always JSON
    report = gc_shared_cache(_policy(shared_cache_root, max_bytes, ttl_hours))
    typer.echo(json.dumps(report.__dict__, indent=2))
    if report.over_limit:
        raise typer.Exit(2)
