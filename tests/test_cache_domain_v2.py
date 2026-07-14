"""Independent shared-cache capacity domain, platform paths, TTL/LRU, and env."""

from __future__ import annotations

import os
import time
from pathlib import Path


def test_default_cache_root_prefers_xdg(monkeypatch, tmp_path: Path) -> None:
    from grok_worker.cache_policy import default_cache_root

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("GROK_WORKER_CACHE_ROOT", raising=False)
    assert default_cache_root() == (tmp_path / "xdg" / "grok-worker").resolve()


def test_default_cache_root_uses_library_caches_on_macos(monkeypatch, tmp_path: Path) -> None:
    import grok_worker.cache_policy as cache_policy

    monkeypatch.delenv("GROK_WORKER_CACHE_ROOT", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(cache_policy, "_platform_name", lambda: "darwin")
    monkeypatch.setattr(cache_policy.Path, "home", lambda: tmp_path)
    assert (
        cache_policy.default_cache_root()
        == (tmp_path / "Library" / "Caches" / "grok-worker").resolve()
    )


def test_shared_cache_environment_covers_python_and_node(tmp_path: Path) -> None:
    from grok_worker.cache_policy import shared_cache_environment

    root = tmp_path / "cache"
    env = shared_cache_environment(root)
    assert env["UV_CACHE_DIR"] == str(root / "uv")
    assert env["PIP_CACHE_DIR"] == str(root / "pip")
    assert env["NPM_CONFIG_CACHE"] == str(root / "npm")
    assert env["POETRY_CACHE_DIR"] == str(root / "poetry")
    assert env["GROK_SHARED_VENV_ROOT"] == str(root / "venvs")
    assert env["PIPENV_VENV_IN_PROJECT"] == "0"
    assert env["POETRY_VIRTUALENVS_IN_PROJECT"] == "false"


def test_cache_ttl_then_lru_respects_protected_keys(tmp_path: Path) -> None:
    from grok_worker.cache_policy import CachePolicy, gc_shared_cache

    root = tmp_path / "cache"
    packs = root / "context-packs"
    venvs = root / "venvs"
    packs.mkdir(parents=True)
    venvs.mkdir(parents=True)
    old = packs / "old-pack.json"
    old.write_bytes(b"o" * 64)
    protected = venvs / "active-fp"
    protected.mkdir()
    (protected / "payload").write_bytes(b"p" * 128)
    lru = venvs / "unused-fp"
    lru.mkdir()
    (lru / "payload").write_bytes(b"u" * 256)
    now = time.time()
    os.utime(old, (now - 10_000, now - 10_000))
    os.utime(protected, (now - 9_000, now - 9_000))
    os.utime(lru, (now - 8_000, now - 8_000))

    report = gc_shared_cache(
        CachePolicy(root=root, max_bytes=180, ttl_hours=2),
        protected={"venvs/active-fp"},
        now=now,
    )
    assert not old.exists()
    assert protected.exists()
    assert not lru.exists()
    assert "venvs/active-fp" in report.protected
    assert report.after_bytes <= report.max_bytes or report.over_limit


def test_cache_over_limit_refuses_when_only_protected_entry_remains(tmp_path: Path) -> None:
    from grok_worker.cache_policy import CacheCapacityError, CachePolicy, ensure_cache_capacity

    root = tmp_path / "cache"
    active = root / "venvs" / "active"
    active.mkdir(parents=True)
    (active / "payload").write_bytes(b"x" * 512)
    try:
        ensure_cache_capacity(
            CachePolicy(root=root, max_bytes=100, ttl_hours=720),
            protected={"venvs/active"},
        )
    except CacheCapacityError as exc:
        assert exc.usage > exc.limit
    else:  # pragma: no cover - RED until capacity domain exists
        raise AssertionError("cache capacity should be enforced independently")


def test_cache_gc_defers_while_worker_lease_is_held(tmp_path: Path) -> None:
    from grok_worker.cache_policy import CachePolicy, cache_use_lease, gc_shared_cache

    root = tmp_path / "cache"
    stale = root / "context-packs" / "stale.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")
    now = time.time()
    os.utime(stale, (now - 10_000, now - 10_000))
    with cache_use_lease(root):
        report = gc_shared_cache(CachePolicy(root=root, max_bytes=1, ttl_hours=1), now=now)
    assert stale.exists()
    assert "active-cache-users" in report.protected


def test_paths_module_uses_platform_cache_policy(monkeypatch, tmp_path: Path) -> None:
    from grok_worker.paths import default_shared_cache_root

    monkeypatch.setenv("GROK_WORKER_CACHE_ROOT", str(tmp_path / "explicit"))
    assert default_shared_cache_root() == (tmp_path / "explicit").resolve()
