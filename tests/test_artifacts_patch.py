"""Patch coverage: git/non-git, dirty, binary, staged, exclusions."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import grok_worker.clone as clone_module
from grok_worker.clone import CloneError, create_workspace
from grok_worker.patch_capture import PatchError, collect_git_patch
from tests.conftest import init_git_repo


def test_patch_covers_changes_and_exclusions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = init_git_repo(repo)

    (repo / "committed.txt").write_text("c\n", encoding="utf-8")
    subprocess.run(["git", "add", "committed.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "commit change"], cwd=repo, check=True, capture_output=True
    )

    (repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("u\n", encoding="utf-8")
    (repo / "data.bin").write_bytes(bytes(range(128)))
    (repo / "empty.txt").write_text("", encoding="utf-8")
    (repo / "weird name.txt").write_text("w\n", encoding="utf-8")

    (repo / ".grok-output").mkdir()
    (repo / ".grok-output" / "result.json").write_text("{}", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "pyvenv.cfg").write_text("x", encoding="utf-8")
    (repo / ".mypy_cache").mkdir()
    (repo / ".mypy_cache" / "c").write_text("x", encoding="utf-8")
    (repo / "prompt-abc.txt").write_text("prompt", encoding="utf-8")

    out = tmp_path / "changes.patch"
    collect_git_patch(repo, base, out)
    text = out.read_text(encoding="utf-8", errors="replace")
    assert "committed.txt" in text
    assert "README.md" in text
    assert "untracked.txt" in text
    assert "empty.txt" in text or "weird name" in text
    raw = out.read_bytes().decode("latin1")
    assert "data.bin" in text or "Binary" in text or "GIT binary" in text or "\0" in raw
    assert ".grok-output" not in text
    assert ".venv" not in text
    assert ".mypy_cache" not in text
    assert "prompt-abc" not in text


def test_patch_raises_on_bad_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_git_repo(repo)
    out = tmp_path / "p.patch"
    with pytest.raises(PatchError):
        collect_git_patch(repo, "0" * 40, out)


def test_staged_only_in_patch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = init_git_repo(repo)
    (repo / "staged.txt").write_text("s\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=repo, check=True, capture_output=True)
    out = tmp_path / "p.patch"
    collect_git_patch(repo, base, out)
    assert "staged.txt" in out.read_text(encoding="utf-8", errors="replace")


def test_nongit_reconstructable_patch(tmp_path: Path) -> None:
    src = tmp_path / "nongit"
    src.mkdir()
    (src / "a.txt").write_text("base\n", encoding="utf-8")
    # preserve symlink as symlink
    (src / "link").symlink_to("a.txt")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, base, _fp, _disc = create_workspace(src, disp, "ng01")
    assert base
    assert (clone / "link").is_symlink()
    (clone / "a.txt").write_text("changed\n", encoding="utf-8")
    (clone / "new.txt").write_text("new\n", encoding="utf-8")
    out = tmp_path / "ng.patch"
    collect_git_patch(clone, base, out)
    text = out.read_text(encoding="utf-8", errors="replace")
    assert "a.txt" in text
    assert "new.txt" in text
    # Sufficient to reconstruct without full clone snapshot
    assert text.strip() != ""


def test_dirty_source_snapshotted_by_default(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_git_repo(src)
    (src / "dirty.txt").write_text("d\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, _base, fp, disclosure = create_workspace(
        src, disp, "d01", include_dirty=False
    )
    assert fp is not None
    assert (clone / "dirty.txt").read_text(encoding="utf-8") == "d\n"
    assert "auto_safe_dirty_snapshot" in disclosure.reason_codes


def test_git_workspace_snapshot_retries_once_and_quarantines_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src-retry"
    disp = tmp_path / "disp-retry"
    init_git_repo(src)
    original = clone_module.create_git_clone
    monkeypatch.setattr(clone_module.tempfile, "gettempdir", lambda: str(tmp_path))
    calls = 0

    def flaky_clone(source: Path, dest: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            (dest / "partial").write_text("incomplete\n", encoding="utf-8")
            raise CloneError("simulated transient clone failure")
        return original(source, dest)

    monkeypatch.setattr(clone_module, "create_git_clone", flaky_clone)
    clone, _base, _fp, _disc = create_workspace(src, disp, "retry01")

    assert calls == 2
    assert (clone / "README.md").is_file()
    assert not (clone / "partial").exists()
    quarantines = list(tmp_path.glob("grok-worker-partial-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "partial").read_text(encoding="utf-8") == "incomplete\n"


def test_dirty_source_include_baseline(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_git_repo(src)
    (src / "dirty.txt").write_text("dirty-input\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, base, fp, disc = create_workspace(
        src, disp, "d02", dirty_allowlist=["dirty.txt"]
    )
    assert fp is not None
    assert disc.risk_decision == "allow"
    assert (clone / "dirty.txt").read_text(encoding="utf-8") == "dirty-input\n"
    # Worker change relative to dirty baseline — not re-emit dirty input alone
    (clone / "worker.txt").write_text("worker-change\n", encoding="utf-8")
    out = tmp_path / "d.patch"
    collect_git_patch(clone, base, out)
    text = out.read_text(encoding="utf-8", errors="replace")
    assert "worker.txt" in text
    # dirty.txt is in baseline; should not appear as new file unless modified
    # (content same as baseline → not in patch)


def test_legacy_bare_include_dirty_snapshots_safe_nonignored(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_git_repo(src)
    (src / "dirty.txt").write_text("dirty-input\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, _base, _fp, disclosure = create_workspace(
        src, disp, "legacy01", include_dirty=True
    )
    assert (clone / "dirty.txt").read_text(encoding="utf-8") == "dirty-input\n"
    assert disclosure.risk_decision == "allow"


def test_dirty_rename_modified_untracked_exact_baseline(tmp_path: Path) -> None:
    """Staged rename + modified + untracked: clone matches; patch excludes dirty input."""
    src = tmp_path / "src"
    init_git_repo(src, filename="oldname.txt", content="v1\n")
    subprocess.run(
        ["git", "-C", str(src), "mv", "oldname.txt", "newname.txt"],
        check=True,
        capture_output=True,
    )
    (src / "newname.txt").write_text("v2-modified\n", encoding="utf-8")
    (src / "untracked-extra.txt").write_text("only-untracked\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, base, fp, _disc = create_workspace(
        src,
        disp,
        "d03",
        dirty_allowlist=["oldname.txt", "newname.txt", "untracked-extra.txt"],
    )
    assert fp is not None
    assert not (clone / "oldname.txt").exists()
    assert (clone / "newname.txt").read_text(encoding="utf-8") == "v2-modified\n"
    assert (clone / "untracked-extra.txt").read_text(encoding="utf-8") == "only-untracked\n"
    # Worker-only change after baseline
    (clone / "worker-only.txt").write_text("from-worker\n", encoding="utf-8")
    out = tmp_path / "d3.patch"
    collect_git_patch(clone, base, out)
    text = out.read_text(encoding="utf-8", errors="replace")
    assert "worker-only.txt" in text
    assert "from-worker" in text
    assert "only-untracked" not in text
    assert "v2-modified" not in text


def test_dirty_baseline_uses_command_scoped_git_identity(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_git_repo(src)
    (src / "dirty.txt").write_text("dirty-input\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, base, fingerprint, _disclosure = create_workspace(
        src,
        disp,
        "d-id",
        dirty_allowlist=["dirty.txt"],
    )
    assert fingerprint is not None

    author = subprocess.check_output(
        ["git", "-C", str(clone), "log", "-1", "--format=%an <%ae>", base],
        text=True,
    ).strip()
    committer = subprocess.check_output(
        ["git", "-C", str(clone), "log", "-1", "--format=%cn <%ce>", base],
        text=True,
    ).strip()
    expected = "grok-worker <grok-worker@localhost>"
    assert author == expected
    assert committer == expected

    for key, owned_value in (
        ("user.name", "grok-worker"),
        ("user.email", "grok-worker@localhost"),
    ):
        configured = subprocess.run(
            ["git", "-C", str(clone), "config", "--local", "--get", key],
            capture_output=True,
            text=True,
            check=False,
        )
        if configured.returncode == 0:
            assert configured.stdout.strip() != owned_value


def test_include_dirty_does_not_copy_gitignored_env(tmp_path: Path) -> None:
    """Regression: ignored .env is never copied into the clone baseline."""
    src = tmp_path / "src"
    init_git_repo(src)
    (src / ".gitignore").write_text(".env\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "add", ".gitignore"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(src), "commit", "-m", "ignore env"],
        check=True,
        capture_output=True,
    )
    (src / "dirty.txt").write_text("tracked-dirty\n", encoding="utf-8")
    ignored_secret = "super-secret-" + "value-never-log"
    (src / ".env").write_text(
        f"SECRET_TOKEN={ignored_secret}\n", encoding="utf-8"
    )
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, _base, _fp, disc = create_workspace(
        src, disp, "ign01", dirty_allowlist=["dirty.txt"]
    )
    assert (clone / "dirty.txt").read_text(encoding="utf-8") == "tracked-dirty\n"
    assert not (clone / ".env").exists()
    assert ignored_secret not in json_safe_disc(disc)


def test_legacy_include_dirty_ignored_only_allows_clean_head(tmp_path: Path) -> None:
    """Bare --include-dirty with only ignored dirt remains clean HEAD (never copies)."""
    src = tmp_path / "src"
    init_git_repo(src)
    (src / ".gitignore").write_text(".env\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "add", ".gitignore"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(src), "commit", "-m", "ignore env"],
        check=True,
        capture_output=True,
    )
    (src / ".env").write_text("SECRET=never\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, _base, _fp, disc = create_workspace(src, disp, "ign-only", include_dirty=True)
    assert not (clone / ".env").exists()
    assert disc.risk_decision == "allow"


def json_safe_disc(disc: object) -> str:
    import json

    from grok_worker.disclosure import DisclosureSummary

    assert isinstance(disc, DisclosureSummary)
    return json.dumps(disc.to_dict())


def test_legacy_dirty_allowlist_no_longer_filters_safe_paths(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_git_repo(src)
    (src / "keep.txt").write_text("k\n", encoding="utf-8")
    (src / "skip.txt").write_text("s\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, _base, _fp, disc = create_workspace(
        src, disp, "al01", dirty_allowlist=["keep.txt"]
    )
    assert (clone / "keep.txt").read_text(encoding="utf-8") == "k\n"
    assert (clone / "skip.txt").read_text(encoding="utf-8") == "s\n"
    assert "keep.txt" in disc.included_dirty_paths
    assert "skip.txt" in disc.included_dirty_paths
    assert "legacy_allowlist_nonfiltering" in disc.reason_codes


def test_materialized_dirty_bytes_are_rescanned_after_source_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src-race"
    disp = tmp_path / "disp-race"
    init_git_repo(src)
    candidate = src / "candidate.txt"
    candidate.write_text("safe\n", encoding="utf-8")
    original = clone_module.create_git_clone
    monkeypatch.setattr(clone_module.tempfile, "gettempdir", lambda: str(tmp_path))

    def mutate_after_clone(source: Path, dest: Path) -> str:
        base = original(source, dest)
        candidate.write_text(
            "api_key=" + "abcdefghijklmnop123456\n", encoding="utf-8"
        )
        return base

    monkeypatch.setattr(clone_module, "create_git_clone", mutate_after_clone)
    with pytest.raises(CloneError, match="sensitive|credential|refused"):
        create_workspace(src, disp, "race01")
    assert not (disp / "grok-worker-race01").exists()


def test_partial_cleanup_refuses_replaced_destination_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src-owner"
    disp = tmp_path / "disp-owner"
    init_git_repo(src)
    monkeypatch.setattr(clone_module.tempfile, "gettempdir", lambda: str(tmp_path))

    def replace_claimed_destination(_source: Path, dest: Path) -> str:
        dest.rmdir()
        dest.mkdir()
        (dest / "external.txt").write_text("do not delete\n", encoding="utf-8")
        raise CloneError("simulated ownership change")

    monkeypatch.setattr(clone_module, "create_git_clone", replace_claimed_destination)
    with pytest.raises(CloneError, match="ownership changed"):
        create_workspace(src, disp, "owner01")
    replacement = disp / "grok-worker-owner01" / "external.txt"
    assert replacement.read_text(encoding="utf-8") == "do not delete\n"


def test_partial_cleanup_never_deletes_replacement_at_original_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src-cleanup-race"
    disp = tmp_path / "disp-cleanup-race"
    init_git_repo(src)
    destination = disp / "grok-worker-cleanrace"

    def fail_clone(_source: Path, dest: Path) -> str:
        (dest / "partial").write_text("owned\n", encoding="utf-8")
        raise CloneError("simulated clone failure")

    monkeypatch.setattr(clone_module.tempfile, "gettempdir", lambda: str(tmp_path))
    original_rename = clone_module.os.rename

    def replace_original_path(source: Path, quarantine: Path) -> None:
        original_rename(source, quarantine)
        if source == destination:
            destination.mkdir()
            (destination / "external.txt").write_text(
                "do not delete\n", encoding="utf-8"
            )

    monkeypatch.setattr(clone_module, "create_git_clone", fail_clone)
    monkeypatch.setattr(clone_module.os, "rename", replace_original_path)
    with pytest.raises(CloneError):
        create_workspace(src, disp, "cleanrace")
    assert (destination / "external.txt").read_text(encoding="utf-8") == "do not delete\n"


def test_dirty_allowlist_rejects_absolute_and_traversal(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_git_repo(src)
    (src / "a.txt").write_text("a\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    with pytest.raises(CloneError, match="absolute|relative|traversal|\\.\\."):
        create_workspace(src, disp, "al02", dirty_allowlist=["/tmp/evil"])
    with pytest.raises(CloneError, match="traversal|\\.\\."):
        create_workspace(src, disp, "al03", dirty_allowlist=["../outside"])


def test_dirty_symlink_escape_refused(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_git_repo(src)
    victim = tmp_path / "victim-secret"
    victim.write_text("host-secret\n", encoding="utf-8")
    link = src / "escape-link"
    link.symlink_to(victim)
    disp = tmp_path / "disp"
    disp.mkdir()
    with pytest.raises(CloneError, match="symlink|refuse|dirty"):
        create_workspace(src, disp, "sym01", dirty_allowlist=["escape-link"])
    assert victim.read_text(encoding="utf-8") == "host-secret\n"


def test_secret_path_refused_without_logging_value(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_git_repo(src)
    secret_body = "AKIA_" + "FAKE_SECRET_VALUE_1234567890"
    (src / "credentials.json").write_text(
        f'{{"api_key": "{secret_body}"}}\n', encoding="utf-8"
    )
    disp = tmp_path / "disp"
    disp.mkdir()
    with pytest.raises(CloneError) as excinfo:
        create_workspace(src, disp, "sec01", dirty_allowlist=["credentials.json"])
    err = str(excinfo.value)
    assert secret_body not in err
    assert "credentials.json" in err or "sensitive" in err.lower() or "refuse" in err.lower()


def test_env_example_path_exempt_from_path_only_refusal(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_git_repo(src)
    (src / ".env.example").write_text("API_KEY=\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, _base, _fp, disc = create_workspace(
        src, disp, "envex", dirty_allowlist=[".env.example"]
    )
    assert (clone / ".env.example").read_text(encoding="utf-8") == "API_KEY=\n"
    assert disc.risk_decision == "allow"


def test_env_example_with_secret_content_refused(tmp_path: Path) -> None:
    src = tmp_path / "src"
    init_git_repo(src)
    secret = "super-" + "secret-token-" + "abcdefgh"
    (src / ".env.example").write_text(f"api_key={secret}\n", encoding="utf-8")
    disp = tmp_path / "disp"
    disp.mkdir()
    with pytest.raises(CloneError) as excinfo:
        create_workspace(src, disp, "envex2", dirty_allowlist=[".env.example"])
    assert secret not in str(excinfo.value)


def test_deleted_sensitive_tracked_path_allowed(tmp_path: Path) -> None:
    """Deleting an already-tracked sensitive-named file is safe (absent path)."""
    src = tmp_path / "src"
    init_git_repo(src, filename="credentials.json", content='{"x":1}\n')
    subprocess.run(
        ["git", "-C", str(src), "rm", "credentials.json"],
        check=True,
        capture_output=True,
    )
    disp = tmp_path / "disp"
    disp.mkdir()
    clone, _base, _fp, disc = create_workspace(
        src, disp, "del-sens", dirty_allowlist=["credentials.json"]
    )
    assert not (clone / "credentials.json").exists()
    assert "credentials.json" in disc.included_dirty_paths
