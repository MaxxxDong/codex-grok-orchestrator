"""Transactional config-apply seam.

Public contract:
- ``grok-worker config-apply --config <path> --candidate <path>
  --smoke-argv-json <JSON array> --smoke-timeout <seconds> --json``
- Candidate must parse as TOML first; apply uses same-dir tempfile + fsync +
  os.replace atomic replace with automatic backup.
- Smoke is a JSON argv exec (no shell). Success keeps the new config; nonzero
  or timeout rolls back atomically at the byte level.
- stdout/stderr/receipt must not leak TOML contents, API keys, or env secrets;
  output is path/hash/exit-code/rollback metadata only.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

from grok_worker.cli import main

SECRET_MARKER = "EXAMPLE_TOKEN_DO_NOT_LEAK"
SECRET_ENV_VALUE = "env-secret-should-never-appear"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_toml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _write_smoke_script(path: Path, *, exit_code: int = 0, delay_seconds: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        f"time.sleep({delay_seconds!r})\n"
        f"sys.exit({int(exit_code)})\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_config_apply_smoke_success_keeps_candidate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: valid TOML candidate + successful smoke retains new config."""
    monkeypatch.setenv("GROK_TEST_SECRET", SECRET_ENV_VALUE)
    config_path = tmp_path / "worker.toml"
    candidate_path = tmp_path / "worker.candidate.toml"
    original = (
        "# original config\n"
        f'api_key = "{SECRET_MARKER}-original"\n'
        'model = "grok-4.5"\n'
    )
    candidate = (
        "# candidate config\n"
        f'api_key = "{SECRET_MARKER}-candidate"\n'
        'model = "grok-4.5"\n'
        "timeout_seconds = 42\n"
    )
    _write_toml(config_path, original)
    _write_toml(candidate_path, candidate)
    original_hash = _sha256(config_path)
    candidate_hash = _sha256(candidate_path)

    smoke = _write_smoke_script(tmp_path / "smoke_ok.py", exit_code=0)
    smoke_argv = json.dumps([sys.executable, str(smoke)])

    code = main(
        [
            "config-apply",
            "--config",
            str(config_path),
            "--candidate",
            str(candidate_path),
            "--smoke-argv-json",
            smoke_argv,
            "--smoke-timeout",
            "5",
            "--json",
        ]
    )
    assert code == 0, "config-apply must succeed when smoke exits 0"
    # Byte-level: live config must now match the candidate contents.
    assert config_path.read_bytes() == candidate_path.read_bytes()
    assert _sha256(config_path) == candidate_hash
    assert _sha256(config_path) != original_hash

    out = capsys.readouterr()
    combined = out.out + out.err
    assert SECRET_MARKER not in combined
    assert SECRET_ENV_VALUE not in combined
    assert "api_key" not in combined.lower() or "api_key" not in out.out.lower()
    # Receipt is metadata only.
    receipt = json.loads(out.out)
    assert isinstance(receipt, dict)
    for forbidden in (SECRET_MARKER, SECRET_ENV_VALUE):
        assert forbidden not in json.dumps(receipt)
    # Expected metadata keys (implementation may add more non-secret fields).
    assert "config_path" in receipt or "path" in receipt or "config" in receipt
    assert any(k in receipt for k in ("smoke_exit_code", "exit_code", "smoke_exit"))
    assert any(k in receipt for k in ("rolled_back", "rollback", "rollback_performed"))


def test_config_apply_smoke_failure_atomically_rolls_back(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary: smoke nonzero → byte-level atomic rollback; no secret leakage."""
    monkeypatch.setenv("GROK_TEST_SECRET", SECRET_ENV_VALUE)
    config_path = tmp_path / "worker.toml"
    candidate_path = tmp_path / "worker.candidate.toml"
    original = (
        'api_key = "original-safe-marker"\n'
        f'token = "{SECRET_MARKER}"\n'
        "enabled = true\n"
    )
    candidate = (
        'api_key = "candidate-should-not-stick"\n'
        f'token = "{SECRET_MARKER}-cand"\n'
        "enabled = false\n"
    )
    _write_toml(config_path, original)
    _write_toml(candidate_path, candidate)
    original_bytes = config_path.read_bytes()
    original_hash = _sha256(config_path)

    smoke = _write_smoke_script(tmp_path / "smoke_fail.py", exit_code=7)
    smoke_argv = json.dumps([sys.executable, str(smoke)])

    code = main(
        [
            "config-apply",
            "--config",
            str(config_path),
            "--candidate",
            str(candidate_path),
            "--smoke-argv-json",
            smoke_argv,
            "--smoke-timeout",
            "5",
            "--json",
        ]
    )
    assert code != 0, "config-apply must fail when smoke exits nonzero"
    # Exact byte-level rollback of the live config.
    assert config_path.read_bytes() == original_bytes
    assert _sha256(config_path) == original_hash

    out = capsys.readouterr()
    combined = out.out + out.err
    assert SECRET_MARKER not in combined
    assert SECRET_ENV_VALUE not in combined
    assert "candidate-should-not-stick" not in combined
    assert "original-safe-marker" not in combined
    # Shell must not be used: smoke argv is a JSON array (enforced by CLI contract).
    # If implementation shells out, secret env expansion could leak; assert no leak.
    if out.out.strip():
        receipt = json.loads(out.out)
        assert isinstance(receipt, dict)
        dumped = json.dumps(receipt)
        assert SECRET_MARKER not in dumped
        assert any(
            k in receipt for k in ("rolled_back", "rollback", "rollback_performed")
        )
        rolled = receipt.get(
            "rolled_back",
            receipt.get("rollback_performed", receipt.get("rollback")),
        )
        assert rolled is True or rolled == "true" or rolled == 1


def test_config_apply_smoke_timeout_atomically_rolls_back(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Boundary: smoke timeout rolls back the exact original bytes."""
    config_path = tmp_path / "worker.toml"
    candidate_path = tmp_path / "worker.candidate.toml"
    original = 'model = "original"\nmarker = "keep-these-bytes"\n'
    candidate = 'model = "candidate"\nmarker = "must-roll-back"\n'
    _write_toml(config_path, original)
    _write_toml(candidate_path, candidate)
    original_bytes = config_path.read_bytes()

    smoke = _write_smoke_script(
        tmp_path / "smoke_timeout.py",
        delay_seconds=0.5,
    )
    smoke_argv = json.dumps([sys.executable, str(smoke)])

    code = main(
        [
            "config-apply",
            "--config",
            str(config_path),
            "--candidate",
            str(candidate_path),
            "--smoke-argv-json",
            smoke_argv,
            "--smoke-timeout",
            "0.05",
            "--json",
        ]
    )

    assert code != 0, "config-apply must fail when smoke times out"
    assert config_path.read_bytes() == original_bytes
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "must-roll-back" not in combined
    if out.out.strip():
        receipt = json.loads(out.out)
        assert receipt.get("rolled_back") is True
        assert receipt.get("timed_out") is True


def test_config_apply_invalid_toml_candidate_refuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Boundary: candidate must parse as TOML before any replace is attempted."""
    config_path = tmp_path / "worker.toml"
    candidate_path = tmp_path / "bad.candidate.toml"
    original = 'model = "grok-4.5"\n'
    _write_toml(config_path, original)
    original_bytes = config_path.read_bytes()
    # Invalid TOML
    candidate_path.write_text('api_key = "unterminated\n[broken\n', encoding="utf-8")

    smoke = _write_smoke_script(tmp_path / "smoke_unused.py", exit_code=0)
    smoke_argv = json.dumps([sys.executable, str(smoke)])

    code = main(
        [
            "config-apply",
            "--config",
            str(config_path),
            "--candidate",
            str(candidate_path),
            "--smoke-argv-json",
            smoke_argv,
            "--smoke-timeout",
            "2",
            "--json",
        ]
    )
    assert code != 0, "invalid TOML candidate must be refused"
    assert config_path.read_bytes() == original_bytes, "live config must be untouched"
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "unterminated" not in combined


def test_config_apply_rejects_non_array_smoke_argv(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Smoke argv must be a JSON array of strings, not a shell string."""
    config_path = tmp_path / "worker.toml"
    candidate_path = tmp_path / "worker.candidate.toml"
    _write_toml(config_path, 'model = "a"\n')
    _write_toml(candidate_path, 'model = "b"\n')
    original = config_path.read_bytes()

    code = main(
        [
            "config-apply",
            "--config",
            str(config_path),
            "--candidate",
            str(candidate_path),
            "--smoke-argv-json",
            '"echo pwned"',
            "--smoke-timeout",
            "2",
            "--json",
        ]
    )
    assert code != 0
    assert config_path.read_bytes() == original
    out = capsys.readouterr()
    if out.out.strip():
        receipt = json.loads(out.out)
        assert receipt.get("applied") is False


def test_config_apply_smoke_stdout_not_leaked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Smoke process stdout/stderr must never appear in the receipt."""
    config_path = tmp_path / "worker.toml"
    candidate_path = tmp_path / "worker.candidate.toml"
    _write_toml(config_path, 'model = "orig"\n')
    _write_toml(candidate_path, 'model = "cand"\n')
    script = tmp_path / "smoke_print.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"print({SECRET_MARKER!r})\n"
        f"print({SECRET_MARKER!r}, file=sys.stderr)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    smoke_argv = json.dumps([sys.executable, str(script)])
    code = main(
        [
            "config-apply",
            "--config",
            str(config_path),
            "--candidate",
            str(candidate_path),
            "--smoke-argv-json",
            smoke_argv,
            "--smoke-timeout",
            "5",
            "--json",
        ]
    )
    assert code == 0
    out = capsys.readouterr()
    combined = out.out + out.err
    assert SECRET_MARKER not in combined


def test_config_apply_backup_bytes_equal_original(tmp_path: Path) -> None:
    """Backup file must be an exact byte copy of the pre-apply live config."""
    from grok_worker.config_apply import apply_config

    config_path = tmp_path / "worker.toml"
    candidate_path = tmp_path / "worker.candidate.toml"
    original = b'model = "original-exact-bytes"\nflag = true\n'
    config_path.write_bytes(original)
    _write_toml(candidate_path, 'model = "candidate"\nflag = false\n')
    smoke = _write_smoke_script(tmp_path / "smoke_ok.py", exit_code=0)
    code, receipt = apply_config(
        config_path=config_path,
        candidate_path=candidate_path,
        smoke_argv_json=json.dumps([sys.executable, str(smoke)]),
        smoke_timeout=5.0,
    )
    assert code == 0
    assert receipt.applied is True
    assert receipt.backup_path is not None
    backup = Path(receipt.backup_path)
    assert backup.is_file()
    assert backup.read_bytes() == original


def test_config_apply_rejects_non_finite_timeout_before_mutation(
    tmp_path: Path,
) -> None:
    """NaN / Inf / 0 / negative timeout must refuse before changing live config."""
    import math

    from grok_worker.config_apply import ConfigApplyError, apply_config

    config_path = tmp_path / "worker.toml"
    candidate_path = tmp_path / "worker.candidate.toml"
    original = b'model = "must-stay"\n'
    config_path.write_bytes(original)
    _write_toml(candidate_path, 'model = "must-not-apply"\n')
    smoke = _write_smoke_script(tmp_path / "smoke_unused.py", exit_code=0)
    smoke_argv = json.dumps([sys.executable, str(smoke)])

    for bad in (math.nan, math.inf, -math.inf, 0.0, -1.0, -0.01):
        with pytest.raises(ConfigApplyError, match="finite positive"):
            apply_config(
                config_path=config_path,
                candidate_path=candidate_path,
                smoke_argv_json=smoke_argv,
                smoke_timeout=bad,
            )
        assert config_path.read_bytes() == original, f"live config mutated for {bad!r}"


def test_config_apply_serializes_concurrent_transactions(tmp_path: Path) -> None:
    """Second apply must not enter replace while the first holds the apply lock during smoke."""
    import threading
    import time

    from grok_worker.config_apply import apply_config

    config_path = tmp_path / "worker.toml"
    cand_a = tmp_path / "a.toml"
    cand_b = tmp_path / "b.toml"
    original = b'model = "original"\n'
    config_path.write_bytes(original)
    _write_toml(cand_a, 'model = "from-a"\n')
    _write_toml(cand_b, 'model = "from-b"\n')

    # Slow smoke for A so B must wait outside the critical section.
    smoke_a = _write_smoke_script(tmp_path / "smoke_a.py", exit_code=0, delay_seconds=0.4)
    smoke_b = _write_smoke_script(tmp_path / "smoke_b.py", exit_code=0, delay_seconds=0.0)

    order: list[str] = []
    results: dict[str, tuple[int, object]] = {}
    barrier = threading.Barrier(2)

    def run_a() -> None:
        barrier.wait(timeout=5)
        order.append("a-start")
        code, receipt = apply_config(
            config_path=config_path,
            candidate_path=cand_a,
            smoke_argv_json=json.dumps([sys.executable, str(smoke_a)]),
            smoke_timeout=5.0,
        )
        order.append("a-end")
        results["a"] = (code, receipt)

    def run_b() -> None:
        barrier.wait(timeout=5)
        # Slight stagger so A likely acquires first, but lock still enforces order.
        time.sleep(0.05)
        order.append("b-start")
        code, receipt = apply_config(
            config_path=config_path,
            candidate_path=cand_b,
            smoke_argv_json=json.dumps([sys.executable, str(smoke_b)]),
            smoke_timeout=5.0,
        )
        order.append("b-end")
        results["b"] = (code, receipt)

    t_a = threading.Thread(target=run_a)
    t_b = threading.Thread(target=run_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)
    assert not t_a.is_alive() and not t_b.is_alive()
    assert results["a"][0] == 0
    assert results["b"][0] == 0
    # Critical section serialization: A must finish before B can finish if A held
    # the lock through its long smoke. B may start waiting while A runs.
    assert order.index("a-end") < order.index("b-end") or order.index("b-end") < order.index(
        "a-end"
    )
    # Stronger: the first to start must fully end before the second ends when
    # smokes are exclusive under the lock — i.e. ends are non-overlapping.
    # With exclusive lock, one of (a-end before b-start) or (b-end before a-start)
    # holds for the critical section. B-start may be recorded before a-end while
    # blocked on lock; require a-end before b-end if a-start precedes b-start.
    if order.index("a-start") < order.index("b-start"):
        assert order.index("a-end") < order.index("b-end")
        # B applied last → live config is B.
        assert config_path.read_text(encoding="utf-8") == cand_b.read_text(encoding="utf-8")
    else:
        assert order.index("b-end") < order.index("a-end")
        assert config_path.read_text(encoding="utf-8") == cand_a.read_text(encoding="utf-8")
