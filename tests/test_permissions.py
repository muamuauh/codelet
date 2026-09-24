"""PermissionGate tests (P1)."""
from __future__ import annotations

from codelet.config import Config, PermissionMode
from codelet.permissions import PermissionGate
from codelet.tools.bash_tool import BashTool
from codelet.tools.base import Tool, ToolResult
from codelet.tools.file_write import FileWriteTool


def test_auto_allows_safe_bash():
    gate = PermissionGate(Config(permission_mode=PermissionMode.AUTO))
    assert gate.check(BashTool(), {"command": "echo hi"}) is None


def test_auto_still_blocks_dangerous_via_layer1():
    gate = PermissionGate(Config(permission_mode=PermissionMode.AUTO))
    result = gate.check(BashTool(), {"command": "rm -rf /"})
    assert result is not None and result.is_error


def test_plan_blocks_writes():
    gate = PermissionGate(Config(permission_mode=PermissionMode.PLAN))
    result = gate.check(FileWriteTool(), {"path": "/tmp/x", "content": "y"})
    assert result is not None and result.is_error
    assert "PLAN" in result.output or "plan" in result.output.lower()


def test_plan_blocks_bash_that_can_write():
    gate = PermissionGate(Config(permission_mode=PermissionMode.PLAN))
    for command in ("rm notes.txt", "echo hi > f.txt", "ls; rm x", "cat a | tee b",
                    "python -c 'open(1)'", "git branch -D main", "find . -delete",
                    "echo $(rm x)", ""):
        result = gate.check(BashTool(), {"command": command})
        assert result is not None and result.is_error, command


def test_plan_allows_read_only_bash():
    """PLAN used to block every bash call, so a read-only session could not even `ls`."""
    gate = PermissionGate(Config(permission_mode=PermissionMode.PLAN))
    for command in ("echo hi", "ls -la", "git status", "git log --oneline -5",
                    "grep -rn TODO src", "find . -name '*.py'"):
        assert gate.check(BashTool(), {"command": command}) is None, command


# ---------- PLAN is an allowlist: undeclared tools count as writing ----------

class _PluginTool(Tool):
    """A plugin tool that never said whether it writes."""

    @property
    def name(self) -> str:
        return "shiny_plugin_tool"

    @property
    def description(self) -> str:
        return "does something"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, params):
        return ToolResult(output="done")


def test_plan_blocks_tools_that_do_not_declare_read_only(tmp_path):
    """Regression: PLAN listed only bash/write_file/edit_file, so plugin tools that
    write -- sandbox, create_tool, memory -- went through a read-only mode."""
    from codelet.plugins.builtin.evolve import CreateToolTool
    from codelet.plugins.builtin.memory import MemoryStore, MemoryTool

    gate = PermissionGate(Config(permission_mode=PermissionMode.PLAN))
    memory = MemoryTool(MemoryStore({"project": tmp_path / "p", "user": tmp_path / "u"}))
    assert gate.check(_PluginTool(), {}) is not None
    assert gate.check(CreateToolTool(None, tmp_path), {"name": "x", "code": "return 1"}) is not None
    assert gate.check(memory, {"action": "write", "key": "k"}) is not None
    assert gate.check(memory, {"action": "view"}) is None            # reading memory is fine


def test_plan_allows_declared_read_only_tools():
    from codelet.tools.file_read import FileReadTool
    from codelet.tools.grep_tool import GrepTool

    gate = PermissionGate(Config(permission_mode=PermissionMode.PLAN))
    assert gate.check(FileReadTool(), {"path": "x"}) is None
    assert gate.check(GrepTool(), {"pattern": "x"}) is None
