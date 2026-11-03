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


def test_prompt_names_the_model_when_given():
    reg = ToolRegistry.default()
    prompt = build_system_prompt(reg, model="qwen/qwen3.7-max")
    assert "qwen/qwen3.7-max" in prompt
    assert "served by the model" in prompt


def test_prompt_omits_model_line_by_default():
    prompt = build_system_prompt(ToolRegistry.default())
    assert "served by the model" not in prompt


def test_environment_section_names_the_shell_and_git_state(tmp_path):
    from codelet.system_prompt import environment_section

    win = environment_section(tmp_path, shell="pwsh")
    assert "PowerShell 7" in win and "Test-Path" in win
    assert "not** a git repository" in win
    assert "cmd.exe" in environment_section(tmp_path, shell="cmd")
    (tmp_path / ".git").mkdir()
    posix = environment_section(tmp_path / "sub", shell="sh")
    assert "/bin/sh" in posix and "is a git repository" in posix


def test_environment_section_is_in_the_prompt():
    prompt = build_system_prompt(ToolRegistry(), permission_mode="auto")
    assert "## Environment" in prompt and "Working directory" in prompt
