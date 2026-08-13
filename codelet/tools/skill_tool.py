"""Skill tool -- on-demand fetch of skill bodies.

The system prompt only carries a compact `name: description` index. When the
model decides a skill is relevant, it calls Skill(name=...) and gets the full
body. This keeps the system prompt small even when many skills are installed.

Skill is read-only and has no side effects -- it's a knowledge-injection tool.
"""
from __future__ import annotations

from typing import Any

from ..skills.loader import SkillIndex
from .base import Tool, ToolResult


class SkillTool(Tool):
    def __init__(self, index: SkillIndex) -> None:
        self._index = index

    @property
    def name(self) -> str:
        return "skill"

    @property
    def description(self) -> str:
        return (
            "Fetch a Skill body by name. Skills are procedural knowledge "
            "(e.g. 'how to do a code review'), injected on demand to keep the "
            "system prompt small. Use /skills (or check the index in the system "
            "prompt) to discover available names."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name from the index."},
            },
            "required": ["name"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        name = (params.get("name") or "").strip()
        if not name:
            return ToolResult(output="Error: 'name' is required.", is_error=True)
        skill = self._index.get(name)
        if skill is None:
            available = ", ".join(self._index.names()) or "(none)"
            return ToolResult(
                output=f"Skill '{name}' not found. Available: {available}",
                is_error=True,
            )
        # Body returned verbatim. The model will read this in the next turn.
        return ToolResult(output=skill.body)
