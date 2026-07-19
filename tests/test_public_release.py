"""Public-release regressions for portability, packaging, and policy separation."""

from __future__ import annotations

import os
import re
import tomllib
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_tree_contains_no_personal_home_paths() -> None:
    offenders: list[str] = []
    ignored_parts = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".uv-cache",
        ".venv",
        "build",
        "dist",
        "__pycache__",
        ".grok-worker",
        ".grok-output",
        ".grok-disposable",
        ".grok-artifacts",
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

    for relative in ("SKILL.md", "docs/design-principles.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert all(name in text for name in ARTIFACT_FILES)


def test_readmes_cross_link_language_versions() -> None:
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "README.zh-CN.md" in english
    assert "README.md" in chinese
    assert all(name in chinese for name in ("changes.patch", "worker.log", "verification.txt"))


def test_readme_is_canonical_bilingual_entry() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pointer = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "## 中文" in readme
    assert "## English" in readme
    assert "codex-grok-orchestrator" in readme
    assert "grok-worker" in readme
    assert "maxxxdong.github.io/codex-grok-orchestrator" in readme
    assert "docs/assets/grok-worker-intro-zh-final.mp4" in readme
    assert "docs/releases/release-notes.md" in readme
    assert all(name in readme for name in ("changes.patch", "worker.log", "verification.txt"))
    assert "stdevMac/grok-in-codex" in readme
    assert "Cjbuilds/Codex-Orchestration" in readme
    assert "No source code" in readme or "未复制" in readme

    assert "README.md" in pointer
    assert "README.md#中文" in pointer
    assert len(pointer.splitlines()) < 40
    assert "session-start" not in pointer


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
    assert "--always-approve" in command
    assert "--no-subagents" not in command


def test_agent_defaults_to_worker_approval_and_subagents(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for key in (
        "GROK_WORKER_MODEL",
        "GROK_WORKER_REASONING_EFFORT",
        "GROK_WORKER_ALLOW_SUBAGENTS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", "grok")

    from grok_worker.agent_entry import build_command

    command = build_command()
    assert "--always-approve" in command
    assert "--no-subagents" not in command
    assert command[command.index("--model") + 1]
    assert command[command.index("--reasoning-effort") + 1]


def test_agent_can_explicitly_disable_subagents(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", "grok")
    monkeypatch.setenv("GROK_WORKER_ALLOW_SUBAGENTS", "0")

    from grok_worker.agent_entry import build_command

    command = build_command()
    assert "--no-subagents" in command


def test_native_command_uses_configured_grok_binary(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", "/opt/tools/custom-grok")

    from grok_worker.run_config import default_grok_bin

    assert default_grok_bin() == "/opt/tools/custom-grok"


def test_native_analysis_is_os_sandboxed_read_only(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GROK_WORKER_GROK_BIN", "grok")
    from grok_worker.run_config import RunConfig, build_native_cmd

    analysis = build_native_cmd(
        RunConfig(source=tmp_path, prompt="review", backend="native", mode="analysis"),
        tmp_path,
        tmp_path / "prompt.md",
    )
    implementation = build_native_cmd(
        RunConfig(source=tmp_path, prompt="edit", backend="native", mode="implementation"),
        tmp_path,
        tmp_path / "prompt.md",
    )

    assert analysis[analysis.index("--sandbox") + 1] == "read-only"
    assert analysis[analysis.index("--permission-mode") + 1] == "plan"
    assert "--always-approve" not in analysis
    assert implementation[implementation.index("--sandbox") + 1] == "workspace"
    assert "--always-approve" in implementation


def test_packaged_prompts_load_without_repository_assets() -> None:
    from grok_worker.prompt_cache import Role, _load_base_and_role

    prompt = _load_base_and_role(None, Role.REVIEW)
    assert "Role: review" in prompt
    assert "configured worker profile" in prompt.lower()


def test_mcp_config_is_optional_in_acpx_command(tmp_path: Path) -> None:
    from grok_worker.run_config import RunConfig, build_acpx_cmd

    cfg = RunConfig(
        source=tmp_path,
        prompt="review",
        backend="acp",
        mcp_config=None,
        model="test-model",
    )
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


def test_release_notes_cover_current_public_release() -> None:
    text = (ROOT / "docs" / "releases" / "release-notes.md").read_text(encoding="utf-8")

    assert "2026-07-19" in text
    assert "0.5.0" in text
    assert "codex-grok-orchestrator" in text
    assert "grok-worker" in text
    assert "changes.patch" in text
    assert "MaxxxDong/codex-grok-orchestrator" in text


class _LandingPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
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
        attr = {key: value for key, value in attrs}
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
        if attr.get("id"):
            self.ids.add(attr["id"] or "")
        if tag == "script":
            self._in_script = True
            self._script_chunks = []
            if attr.get("src"):
                self.external_scripts += 1
        if tag == "link" and attr.get("rel") == "stylesheet" and attr.get("href"):
            href = attr["href"] or ""
            if href.startswith(("http://", "https://")):
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
    text = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
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
    assert parser.script_bodies

    for forbidden in (
        "/Users/" + "max",
        "api_key=",
        "Authorization: Bearer",
        "relay.internal",
    ):
        assert forbidden not in text
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
