"""Two-layer permission gate.

Layer 1: Tool.check_permissions() -- per-tool self-check (e.g. dangerous bash patterns)
Layer 2: PermissionMode -- ASK / AUTO / PLAN

P4 will add a third layer: settings.json allow/deny rules + hooks.
"""
from __future__ import annotations

from typing import Any

from .config import Config, PermissionMode
from .tools.base import Tool, ToolResult


class PermissionGate:
    def __init__(self, config: Config) -> None:
        self.config = config

    def check(self, tool: Tool, params: dict[str, Any]) -> ToolResult | None:
        """Return a ToolResult with is_error=True if denied, otherwise None."""
        # Layer 1: tool self-check
        denial = tool.check_permissions(params)
        if denial is not None:
            return ToolResult(output=f"Permission denied: {denial}", is_error=True)

        mode = self.config.permission_mode

        # Layer 2: mode-based filtering
        if mode == PermissionMode.PLAN:
            write_tools = {"bash", "write_file", "edit_file"}
            if tool.name in write_tools:
                return ToolResult(
                    output=f"Permission denied: '{tool.name}' is blocked in PLAN (read-only) mode.",
                    is_error=True,
                )

        if mode == PermissionMode.ASK:
            if tool.name == "bash":
                cmd = params.get("command", "")
                if not self._is_safe_command(cmd):
                    if not self._ask_user(tool.name, params):
                        return ToolResult(output="Permission denied: user rejected.", is_error=True)

        return None

    def _is_safe_command(self, command: str) -> bool:
        cmd_lower = command.strip().lower()
        return any(cmd_lower.startswith(safe) for safe in self.config.allowed_commands)

    @staticmethod
    def _ask_user(tool_name: str, params: dict[str, Any]) -> bool:
        detail = ""
        if tool_name == "bash":
            detail = params.get("command", "")
        elif tool_name in ("write_file", "edit_file"):
            detail = params.get("path", "")
        prompt = f"\n[Permission] Allow '{tool_name}'"
        if detail:
            prompt += f": {detail}"
        prompt += "? [y/N] "
        try:
            answer = input(prompt).strip().lower()
            return answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False
