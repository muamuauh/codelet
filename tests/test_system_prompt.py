"""System prompt builder tests."""
from __future__ import annotations

from codelet.system_prompt import build_system_prompt
from codelet.tools.base import ToolRegistry


def test_prompt_contains_tool_names():
    reg = ToolRegistry.default()
    prompt = build_system_prompt(reg, permission_mode="ask")
    for name in ("bash", "read_file", "write_file", "edit_file", "glob", "grep"):
        assert name in prompt


def test_prompt_reflects_mode():
    reg = ToolRegistry.default()
    plan_prompt = build_system_prompt(reg, permission_mode="plan")
    assert "PLAN" in plan_prompt
    assert "read-only" in plan_prompt.lower()
