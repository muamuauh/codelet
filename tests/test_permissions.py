"""PermissionGate tests (P1)."""
from __future__ import annotations

from codelet.config import Config, PermissionMode
from codelet.permissions import PermissionGate
from codelet.tools.bash_tool import BashTool
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


def test_plan_blocks_bash():
    gate = PermissionGate(Config(permission_mode=PermissionMode.PLAN))
    result = gate.check(BashTool(), {"command": "echo hi"})
    assert result is not None and result.is_error
