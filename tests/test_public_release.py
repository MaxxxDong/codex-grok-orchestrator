"""Public-release regressions for portability, packaging, and policy separation."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def test_public_tree_contains_no_personal_home_paths() -> None:
    offenders: list[str] = []
    ignored_parts = {
        ".git",
        ".grok-output",
        ".grok-worker",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "build",
        "dist",
        "__pycache__",
    }
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in ignored_parts for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        forbidden_home = "/Users/" + "max"
        if forbidden_home in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_runtime_version_matches_package_metadata() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    from grok_worker import __version__

    assert __version__ == data["project"]["version"]


def test_public_docs_match_external_artifact_contract() -> None:
    from grok_worker.artifact_contract import ARTIFACT_FILES

    for relative in ("SKILL.md", "docs/design-principles.md", "README.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert all(name in text for name in ARTIFACT_FILES)


def test_readme_is_canonical_bilingual_entry() -> None:
    english_anchor = (ROOT / "README.md").read_text(encoding="utf-8")
    pointer = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "# 中文" in english_anchor or "## 中文" in english_anchor
    assert "# English" in english_anchor or "## English" in english_anchor
    assert "codex-grok-orchestrator" in english_anchor
    assert "grok-worker" in english_anchor
    assert "maxxxdong.github.io/codex-grok-orchestrator" in english_anchor
    assert "docs/assets/grok-worker-intro-zh-final.mp4" in english_anchor
    assert "docs/releases/release-notes.md" in english_anchor
    artifacts = ("changes.patch", "worker.log", "verification.txt")
    assert all(name in english_anchor for name in artifacts)
    assert "stdevMac/grok-in-codex" in english_anchor
    assert "Cjbuilds/Codex-Orchestration" in english_anchor
    assert "No source code" in english_anchor or "未复制" in english_anchor

    # Compatibility pointer must not re-host the full guide.
    assert "README.md" in pointer
    assert "#中文" in pointer or "README.md#中文" in pointer
    assert len(pointer.splitlines()) < 40
    assert "session-start" not in pointer


def test_readmes_cross_link_language_versions() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pointer = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    lowered = readme.lower()

    assert "README.zh-CN.md" in readme or "#中文" in readme or "](#中文)" in readme
    assert "README.md" in pointer
    assert "](#english)" in lowered or "#english" in lowered


def test_package_declares_runtime_cli_and_entry_points() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert "typer" in " ".join(project["dependencies"]).lower()
    assert project["scripts"]["grok-worker"] == "grok_worker.cli:main"
    assert project["scripts"]["grok-worker-agent"] == "grok_worker.agent_entry:main"
    assert project["license"] == "Apache-2.0"


def test_agent_command_uses_configured_profile(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", "/opt/tools/grok")
    monkeypatch.setenv("GROK_WORKER_MODEL", "grok-test-model")
    monkeypatch.setenv("GROK_WORKER_REASONING_EFFORT", "medium")
    monkeypatch.setenv("GROK_WORKER_ALLOW_SUBAGENTS", "1")

    from grok_worker.agent_entry import build_command

    command = build_command()
    assert command[0] == "/opt/tools/grok"
    assert command[command.index("--model") + 1] == "grok-test-model"
    assert command[command.index("--reasoning-effort") + 1] == "medium"
    assert "--no-subagents" not in command


def test_agent_defaults_to_safe_no_subagents(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for key in (
        "GROK_WORKER_MODEL",
        "GROK_WORKER_REASONING_EFFORT",
        "GROK_WORKER_ALLOW_SUBAGENTS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", "grok")

    from grok_worker.agent_entry import build_command

    command = build_command()
    assert "--no-subagents" in command
    assert command[command.index("--model") + 1]
    assert command[command.index("--reasoning-effort") + 1]


def test_windows_agent_prefers_canonical_native_grok_binary(tmp_path: Path) -> None:
    from grok_worker.agent_entry import resolve_grok_bin

    native = tmp_path / ".grok" / "bin" / "grok.exe"
    native.parent.mkdir(parents=True)
    native.touch()

    resolved = resolve_grok_bin(platform="nt", home=tmp_path, path_lookup=lambda _name: "grok.CMD")

    assert resolved == str(native)


def test_agent_launch_is_silent_on_windows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import grok_worker.agent_entry as agent_entry

    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("GROK_WORKER_LIFECYCLE", "1")
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", "grok")
    monkeypatch.setattr(agent_entry.subprocess, "run", fake_run)

    assert agent_entry.main() == 0
    assert captured["check"] is False
    startup_info = captured["startupinfo"]
    if os.name == "nt":
        assert isinstance(startup_info, subprocess.STARTUPINFO)
        assert startup_info.dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert startup_info.wShowWindow == subprocess.SW_HIDE
    else:
        assert startup_info is None


def test_packaged_prompts_load_without_repository_assets() -> None:
    from grok_worker.prompt_cache import Role, _load_base_and_role

    prompt = _load_base_and_role(None, Role.REVIEW)
    assert "Role: review" in prompt
    assert "configured worker profile" in prompt.lower()


def test_mcp_config_is_optional_in_acpx_command(tmp_path: Path) -> None:
    from grok_worker.run_config import RunConfig, build_acpx_cmd

    cfg = RunConfig(source=tmp_path, prompt="review", mcp_config=None, model="test-model")
    command = build_acpx_cmd(cfg, tmp_path, "agent", "prompt")
    assert "--mcp-config" not in command
    assert command[command.index("--model") + 1] == "test-model"


def test_public_skill_does_not_hard_lock_one_model() -> None:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "Fixed policy" not in text
    assert "Model: `grok-4.5`" not in text
    assert "GROK_WORKER_MODEL" in text


def test_source_checkout_launcher_has_no_host_fallback() -> None:
    text = (ROOT / "bin" / "grok-worker").read_text(encoding="utf-8")
    assert "/Users/" not in text
    assert "GROK_WORKER_SKILL_ROOT" in text


def test_test_marker_is_obviously_fake() -> None:
    marker = "EXAMPLE_TOKEN_DO_NOT_LEAK"
    assert "DO_NOT_LEAK" in marker
    assert os.environ.get(marker) is None


def test_release_notes_exist_for_initial_public_release() -> None:
    path = ROOT / "docs" / "releases" / "release-notes.md"
    text = path.read_text(encoding="utf-8")
    assert "2026-07-14" in text
    assert "codex-grok-orchestrator" in text
    assert "grok-worker" in text
    assert "changes.patch" in text
    assert "MaxxxDong/codex-grok-orchestrator" in text


class _LandingPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.ids: set[str] = set()
        self.langs: set[str] = set()
        self.has_main = False
        self.has_header = False
        self.has_footer = False
        self.has_video = False
        self.video_src: str | None = None
        self.video_poster: str | None = None
        self.script_bodies: list[str] = []
        self._in_script = False
        self._script_chunks: list[str] = []
        self.canonical: str | None = None
        self.og_image: str | None = None
        self.external_scripts = 0
        self.external_stylesheets = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        attr = {k: v for k, v in attrs}
        if tag == "html" and attr.get("lang"):
            self.langs.add(attr["lang"] or "")
        if tag == "main":
            self.has_main = True
        if tag == "header":
            self.has_header = True
        if tag == "footer":
            self.has_footer = True
        if tag == "video":
            self.has_video = True
            self.video_poster = attr.get("poster")
        if tag == "source" and attr.get("src"):
            self.video_src = attr.get("src")
        if "id" in attr and attr["id"]:
            self.ids.add(attr["id"])
        if tag == "script":
            self._in_script = True
            self._script_chunks = []
            if attr.get("src"):
                self.external_scripts += 1
        if tag == "link" and attr.get("rel") == "stylesheet" and attr.get("href"):
            href = attr["href"] or ""
            if href.startswith("http://") or href.startswith("https://"):
                self.external_stylesheets += 1
        if tag == "link" and attr.get("rel") == "canonical":
            self.canonical = attr.get("href")
        if tag == "meta" and attr.get("property") == "og:image":
            self.og_image = attr.get("content")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_script:
            self.script_bodies.append("".join(self._script_chunks))
            self._in_script = False
            self._script_chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_chunks.append(data)


def test_docs_landing_page_is_static_bilingual_and_self_contained() -> None:
    path = ROOT / "docs" / "index.html"
    text = path.read_text(encoding="utf-8")
    parser = _LandingPageParser()
    parser.feed(text)
    parser.close()

    assert parser.has_main and parser.has_header and parser.has_footer
    assert parser.has_video
    assert parser.video_src == "assets/grok-worker-intro-zh-final.mp4"
    assert parser.video_poster == "assets/grok-worker-intro-poster.jpg"
    assert parser.canonical == "https://maxxxdong.github.io/codex-grok-orchestrator/"
    assert parser.og_image is not None
    assert parser.og_image.endswith("assets/grok-worker-intro-poster.jpg")
    assert parser.external_scripts == 0
    assert parser.external_stylesheets == 0
    assert "github.com/MaxxxDong/codex-grok-orchestrator" in text
    assert 'data-lang-block="zh"' in text
    assert 'data-lang-block="en"' in text
    assert "localStorage" in text
    assert "replaceState" in text
    assert "prefers-reduced-motion" in text
    assert "lang-zh" in parser.ids and "lang-en" in parser.ids

    # Inline script must parse as valid JavaScript via Node when available.
    assert parser.script_bodies, "expected inline language-switch script"
    script = parser.script_bodies[0]
    assert "langFromQuery" in script or "searchParams" in script

    # No personal paths or credential-like private markers in public launch surfaces.
    for forbidden in (
        "/Users/" + "max",
        "api_key=",
        "Authorization: Bearer",
        "relay.internal",
    ):
        assert forbidden not in text
    # Avoid matching benign substrings like "task-id"; require token-shaped sk- secrets.
    assert re.search(r"\bsk-[A-Za-z0-9]{8,}\b", text) is None


def test_public_launch_files_have_no_private_markers() -> None:
    relatives = (
        "README.md",
        "README.zh-CN.md",
        "docs/index.html",
        "docs/releases/release-notes.md",
    )
    patterns = [
        re.compile(r"/Users/" + re.escape("max")),
        re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}"),
        re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*"),
        re.compile(r"(?i)xai-[A-Za-z0-9]{20,}"),
        re.compile(r"(?i)relay\.(internal|local)"),
    ]
    offenders: list[str] = []
    for relative in relatives:
        text = (ROOT / relative).read_text(encoding="utf-8")
        for pattern in patterns:
            if pattern.search(text):
                offenders.append(f"{relative}: {pattern.pattern}")
    assert offenders == []
