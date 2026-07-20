"""Task-scoped tool policy for native Grok flags (no prefill/profile layer)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolPolicy:
    """Opt-in pure-code constraints; default keeps plugins/MCP/web available."""

    disable_web_search: bool = False
    disallowed_tools: tuple[str, ...] = ()
    allow_subagents: bool = True

    def signature(self) -> str:
        payload = {
            "disable_web_search": self.disable_web_search,
            "disallowed_tools": list(self.disallowed_tools),
            "allow_subagents": self.allow_subagents,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "disable_web_search": self.disable_web_search,
            "disallowed_tools": list(self.disallowed_tools),
            "allow_subagents": self.allow_subagents,
            "signature": self.signature(),
        }

    @classmethod
    def from_fields(
        cls,
        *,
        disable_web_search: bool = False,
        disallowed_tools: list[str] | tuple[str, ...] | None = None,
        allow_subagents: bool = True,
    ) -> ToolPolicy:
        tools: list[str] = []
        seen: set[str] = set()
        for item in disallowed_tools or ():
            name = str(item).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            tools.append(name)
        return cls(
            disable_web_search=bool(disable_web_search),
            disallowed_tools=tuple(tools),
            allow_subagents=bool(allow_subagents),
        )


def apply_native_tool_flags(cmd: list[str], policy: ToolPolicy) -> list[str]:
    """Append supported native Grok tool-policy flags."""
    out = list(cmd)
    if policy.disable_web_search:
        out.append("--disable-web-search")
    if policy.disallowed_tools:
        out.extend(["--disallowed-tools", ",".join(policy.disallowed_tools)])
    if not policy.allow_subagents:
        if "--no-subagents" not in out:
            out.append("--no-subagents")
    return out


__all__ = ["ToolPolicy", "apply_native_tool_flags"]
