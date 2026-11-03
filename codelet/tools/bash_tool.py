"""Bash tool -- shell with deny-pattern guard.

Which shell actually runs the command (`resolve_shell`):
  * Windows: PowerShell 7 (`pwsh`) when it is on PATH, else cmd.exe. cmd was
    the old default (`shell=True`) and the first eval suite paid for it: POSIX
    habits like `[ -f x ]`, heredocs and `VAR=1 cmd` all fail there, and its
    error messages arrived as mojibake. pwsh has `&&`, `||`, real quoting and
    UTF-8 output, and the model knows its syntax.
  * elsewhere: /bin/sh via `shell=True`.
  * `CODELET_SHELL=pwsh|cmd|sh` overrides the choice.

The tool keeps the name `bash` -- prompts, evals and plugins refer to it -- and
the system prompt's Environment section says which shell is really behind it.
"""
from __future__ import annotations

import base64
import locale
import os
import re
import shlex
import shutil
import subprocess
from typing import Any

from .base import Tool, ToolResult


# Commands that only read. One command per call: any shell operator (pipe,
# redirect, chaining, substitution) makes the call count as writing, because
# `cat a > b` or `ls; rm x` would otherwise slip through.
READ_ONLY_COMMANDS = {"ls", "dir", "cat", "head", "tail", "wc", "find", "grep", "rg",
                      "tree", "pwd", "which", "where", "echo", "date", "stat", "file", "du",
                      # PowerShell cmdlets and their aliases that only read
                      "get-childitem", "gci", "get-content", "gc", "type", "select-string",
                      "sls", "test-path", "get-location", "gl", "resolve-path", "get-item",
                      "gi", "get-date", "write-output", "get-command", "gcm"}
READ_ONLY_GIT = {"status", "diff", "log", "show", "blame", "branch", "remote", "rev-parse"}
_SHELL_OPERATORS = re.compile(r"[;&|<>`]|\$\(")


# Prepended to every pwsh command: UTF-8 both ways, no progress records, and
# no ANSI colour codes (Select-String highlights matches with them).
_PWSH_PRELUDE = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                 "$OutputEncoding = [System.Text.Encoding]::UTF8; "
                 "$ProgressPreference = 'SilentlyContinue'; "
                 "$PSStyle.OutputRendering = 'PlainText'\n")
# Appended: report failure the way a shell would. `$?` is false when the last
# statement failed -- a cmdlet error or a native command's non-zero exit -- and
# then the native exit code is passed through when there is one.
_PWSH_EPILOGUE = "\nif (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 } }"


def resolve_shell() -> tuple[str, str | None]:
    """(kind, executable) for running commands: ("pwsh", path) / ("cmd", None) / ("sh", None)."""
    choice = os.environ.get("CODELET_SHELL", "auto").strip().lower()
    if choice == "pwsh" or (choice == "auto" and os.name == "nt"):
        exe = shutil.which("pwsh")
        if exe:
            return "pwsh", exe
    if choice == "sh" or os.name != "nt":
        return "sh", None
    return "cmd", None


def _pwsh_argv(exe: str, command: str) -> list[str]:
    """-EncodedCommand (base64 UTF-16LE) sidesteps every quoting rule between
    Python, CreateProcess and PowerShell's own parser."""
    # A command that starts with a quoted path ("C:\\Program Files\\x.exe" -v) is a
    # string expression in PowerShell, not a call; `&` makes it a call. Habit
    # from every other shell, so fix it here rather than fail the step.
    if command.lstrip()[:1] in ("'", '"'):
        command = "& " + command.lstrip()
    script = _PWSH_PRELUDE + command + _PWSH_EPILOGUE
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    # -OutputFormat Text: with redirected pipes pwsh otherwise serialises the
    # error stream as CLIXML, which the model cannot read.
    return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text",
            "-EncodedCommand", encoded]


def decode_output(data: bytes | None) -> str:
    """Bytes from a child process -> text the model can actually read.

    Python children (PYTHONUTF8=1) write UTF-8, but cmd.exe writes its own
    messages in the OEM code page -- cp936 on a Chinese Windows. Decoding
    everything as UTF-8 with errors="replace" turned "'rg' 不是内部或外部命令"
    into "'rg' ����ڲ�…": the model was told a command failed and not why,
    which is how the first eval suite saw the same POSIX mistakes repeat.
    Strict UTF-8 first, then the console's code pages, then UTF-8 with
    replacement as the last resort.
    """
    if not data:
        return ""
    candidates = ["utf-8"]
    if os.name == "nt":
        candidates.append("oem")
    candidates.append(locale.getpreferredencoding(False) or "utf-8")
    for enc in candidates:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


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
        "remove-item -recurse -force c:\\", "remove-item -recurse -force /",
        "format-volume", "stop-computer", "restart-computer",
    ]

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        kind, _ = resolve_shell()
        where = {"pwsh": " Commands run in PowerShell 7 (pwsh) -- use PowerShell syntax.",
                 "cmd": " Commands run in cmd.exe.",
                 "sh": ""}[kind]
        return (
            "Execute a shell command. Use for running scripts, installing packages, "
            "git operations, and any shell task. Commands run in the current working directory."
            + where
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
            kind, exe = resolve_shell()
            if kind == "pwsh":
                result = subprocess.run(_pwsh_argv(exe, command), capture_output=True,
                                        timeout=120)
            else:
                result = subprocess.run(command, shell=True, capture_output=True, timeout=120)
            stdout, stderr = decode_output(result.stdout), decode_output(result.stderr)
            parts: list[str] = []
            if stdout:
                parts.append(stdout)
            if stderr:
                parts.append(f"STDERR:\n{stderr}")
            output = "\n".join(parts) or "(no output)"
            if len(output) > 50_000:
                output = output[:50_000] + "\n... (truncated)"
            return ToolResult(output=output, is_error=result.returncode != 0)
        except subprocess.TimeoutExpired:
            return ToolResult(output="Error: command timed out after 120s", is_error=True)
        except Exception as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)
