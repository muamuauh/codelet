"""Two-layer permission gate.

Layer 1: Tool.check_permissions() -- per-tool self-check (e.g. dangerous bash patterns)
Layer 2: PermissionMode -- NORMAL / ASK / AUTO / PLAN
"""
from __future__ import annotations

from typing import Any

from .config import Config, PermissionMode
from .tools.base import Tool, ToolResult


class PermissionGate:
    def __init__(self, config: Config) -> None:
        self.config = config

    def check(self, tool: Tool, params: dict[str, Any], *, defer_confirm: bool = False) -> ToolResult | None:
        """Return a ToolResult with is_error=True if denied, otherwise None.

        When `defer_confirm=True` (used by AgentLoop which routes interactive
        approvals through its sync/async `confirm_callback`), shell confirmation
        is handled by `needs_bash_confirmation()` instead of blocking on `input()`.
        """
        # Layer 1: tool self-check (always enforced, even in AUTO)
        denial = tool.check_permissions(params)
        if denial is not None:
            return ToolResult(output=f"Permission denied: {denial}", is_error=True)

        mode = self.config.permission_mode

        # Layer 2: mode-based filtering
        if mode == PermissionMode.PLAN:
            if not tool.is_read_only(params):
                return ToolResult(
                    output=f"Permission denied: '{tool.name}' can change files, which "
                           "PLAN (read-only) mode does not allow.",
                    is_error=True,
                )

        if not defer_confirm and self.needs_bash_confirmation(tool, params):
            if not self._ask_user(tool.name, params):
                return ToolResult(output="Permission denied: user rejected.", is_error=True)

        return None

    def needs_bash_confirmation(self, tool: Tool, params: dict[str, Any]) -> bool:
        """True when a bash command should prompt the user before running."""
        if tool.name != "bash":
            return False
        mode = self.config.permission_mode
        if mode == PermissionMode.ASK:
            # Strict: every non-read-only shell command asks for confirmation.
            return not tool.is_read_only(params)
        if mode == PermissionMode.NORMAL:
            # Balanced: common dev commands in allowed_commands run freely; others ask.
            cmd = params.get("command", "")
            return not self._is_safe_command(cmd)
        return False

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
