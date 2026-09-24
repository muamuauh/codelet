"""Bash tool -- shell with deny-pattern guard.

Windows note: relies on `shell=True`; on Windows that means cmd.exe. PowerShell
commands work via `powershell -Command "..."` if needed.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from typing import Any

from .base import Tool, ToolResult


# Commands that only read. One command per call: any shell operator (pipe,
# redirect, chaining, substitution) makes the call count as writing, because
# `cat a > b` or `ls; rm x` would otherwise slip through.
READ_ONLY_COMMANDS = {"ls", "dir", "cat", "head", "tail", "wc", "find", "grep", "rg",
                      "tree", "pwd", "which", "where", "echo", "date", "stat", "file", "du"}
READ_ONLY_GIT = {"status", "diff", "log", "show", "blame", "branch", "remote", "rev-parse"}
_SHELL_OPERATORS = re.compile(r"[;&|<>`]|\$\(")


def is_read_only_command(command: str) -> bool:
    if not command.strip() or _SHELL_OPERATORS.search(command):
        return False
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return False
    head = argv[0].lower()
    if head == "git":
        sub = [a for a in argv[1:] if not a.startswith("-")]
        return bool(sub) and sub[0] in READ_ONLY_GIT and "-D" not in argv and "-d" not in argv
    if head == "find":
        return not any(a in ("-delete", "-exec", "-execdir", "-ok", "-fprint") for a in argv)
    return head in READ_ONLY_COMMANDS


class BashTool(Tool):
    def is_read_only(self, params: dict[str, Any]) -> bool:
        return is_read_only_command(str(params.get("command", "")))

    DANGEROUS_PATTERNS = [
        "rm -rf /", "rm -rf ~", "sudo rm",
        "git push --force", "git reset --hard",
        "> /dev/sda", "mkfs", "dd if=",
        ":(){ :|:& };:",
        "format c:", "del /f /s /q c:\\",
    ]

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command. Use for running scripts, installing packages, "
            "git operations, and any shell task. Commands run in the current working directory."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute."},
            },
            "required": ["command"],
        }

    def check_permissions(self, params: dict[str, Any]) -> str | None:
        cmd = params.get("command", "").lower()
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern in cmd:
                return f"Blocked: command matches dangerous pattern '{pattern}'"
        return None

    def execute(self, params: dict[str, Any]) -> ToolResult:
        command = params.get("command", "")
        if not command.strip():
            return ToolResult(output="Error: empty command", is_error=True)
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            parts: list[str] = []
            if result.stdout:
                parts.append(result.stdout)
            if result.stderr:
                parts.append(f"STDERR:\n{result.stderr}")
            output = "\n".join(parts) or "(no output)"
            if len(output) > 50_000:
                output = output[:50_000] + "\n... (truncated)"
            return ToolResult(output=output, is_error=result.returncode != 0)
        except subprocess.TimeoutExpired:
            return ToolResult(output="Error: command timed out after 120s", is_error=True)
        except Exception as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)
