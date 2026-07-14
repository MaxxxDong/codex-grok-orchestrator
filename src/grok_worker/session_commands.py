"""Pure acpx named-session command builders."""

from __future__ import annotations

from pathlib import Path


def build_ensure_cmd(common: list[str], session_name: str) -> list[str]:
    return [*common, "sessions", "ensure", "--name", session_name]


def build_prompt_cmd(common: list[str], session_name: str, prompt_file: Path) -> list[str]:
    return [*common, "prompt", "--session", session_name, "--file", str(prompt_file)]


def build_close_cmd(common: list[str], session_name: str) -> list[str]:
    return [*common, "sessions", "close", session_name]
