"""Agent wrapper policy: native profile; hard-fail without lifecycle env."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]


def test_wrapper_policy_contents() -> None:
    wrapper = (CANDIDATE / "bin" / "grok-acp-worker").read_text(encoding="utf-8")
    entry = (CANDIDATE / "src" / "grok_worker" / "agent_entry.py").read_text(
        encoding="utf-8"
    )
    assert "GROK_WORKER_LIFECYCLE" in entry
    assert "GROK_WORKER_MODEL" in entry or "default_model" in entry
    assert "GROK_WORKER_ALLOW_SUBAGENTS" in entry
    assert "--no-subagents" in entry
    assert "--always-approve" in entry
    assert "check_grok_environment" in entry
    assert "prepare_isolated_profile" not in entry
    assert "grok_worker.agent_entry" in wrapper


def test_wrapper_hard_fail_without_lifecycle() -> None:
    env = os.environ.copy()
    env.pop("GROK_WORKER_LIFECYCLE", None)
    env.pop("GROK_WORKER_ALLOW_DIRECT_AGENT", None)
    proc = subprocess.run(
        [str(CANDIDATE / "bin" / "grok-acp-worker")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 2
    assert "refusing" in proc.stderr.lower() or "GROK_WORKER_LIFECYCLE" in proc.stderr


def test_launcher_uses_platform_cache_contract() -> None:
    text = (CANDIDATE / "bin" / "grok-worker").read_text(encoding="utf-8")
    assert "GROK_WORKER_CACHE_ROOT" in text
    assert "Library/Caches/grok-worker" in text
    assert "uname -s" in text
    assert "ensure_private_writable_dir" in text
    assert '[ -O "$directory" ]' in text
    assert 'chmod 700 "$directory"' in text
    assert '"$ROOT/.venv/bin/python"' in text
    assert "${TMPDIR:-/tmp}/grok-worker-$(id -u)" in text
    assert 'export GROK_WORKER_CACHE_ROOT="$CACHE_ROOT"' in text


def _fake_uv(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "printf '%s|%s|%s\\n' \"$GROK_WORKER_CACHE_ROOT\" \"$UV_CACHE_DIR\" \"$*\"\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    for name in ("python3.14", "python3.13", "python3.12", "python3"):
        candidate = fake_bin / name
        candidate.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        candidate.chmod(0o755)
    return fake_bin


def _launcher_without_venv(tmp_path: Path) -> Path:
    root = tmp_path / "skill"
    (root / "bin").mkdir(parents=True)
    (root / "src/grok_worker").mkdir(parents=True)
    launcher = root / "bin/grok-worker"
    launcher.write_bytes((CANDIDATE / "bin/grok-worker").read_bytes())
    launcher.chmod(0o755)
    return launcher


def test_launcher_falls_back_when_default_cache_is_unwritable(tmp_path: Path) -> None:
    fake_bin = _fake_uv(tmp_path)
    launcher = _launcher_without_venv(tmp_path)
    blocked_home = tmp_path / "blocked-home"
    blocked_home.write_text("not a directory\n", encoding="utf-8")
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(blocked_home),
            "TMPDIR": str(temporary),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    for name in ("GROK_WORKER_CACHE_ROOT", "UV_CACHE_DIR", "XDG_CACHE_HOME"):
        env.pop(name, None)

    proc = subprocess.run(
        [str(launcher), "--version"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    expected = temporary / f"grok-worker-{os.getuid()}"
    assert proc.returncode == 0
    assert proc.stdout.startswith(f"{expected}|{expected / 'uv'}|")
    assert "default cache was not writable" in proc.stderr


def test_launcher_rejects_explicit_unwritable_cache(tmp_path: Path) -> None:
    fake_bin = _fake_uv(tmp_path)
    launcher = _launcher_without_venv(tmp_path)
    blocked = tmp_path / "explicit-cache"
    blocked.write_text("not a directory\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "GROK_WORKER_CACHE_ROOT": str(blocked),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )

    proc = subprocess.run(
        [str(launcher), "--version"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 1
    assert "configured cache root is not writable" in proc.stderr


def test_launcher_rejects_explicit_symlink_cache(tmp_path: Path) -> None:
    fake_bin = _fake_uv(tmp_path)
    launcher = _launcher_without_venv(tmp_path)
    actual = tmp_path / "actual-cache"
    actual.mkdir()
    linked = tmp_path / "linked-cache"
    linked.symlink_to(actual, target_is_directory=True)
    env = os.environ.copy()
    env.update(
        {
            "GROK_WORKER_CACHE_ROOT": str(linked),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )

    proc = subprocess.run(
        [str(launcher), "--version"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 1
    assert "configured cache root is not writable" in proc.stderr
