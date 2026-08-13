"""Hook runner -- shell-based extension points.

Three events fire during an agent session:

    PreToolUse       fires BEFORE a tool executes
    PostToolUse      fires AFTER  a tool executes
    UserPromptSubmit fires when the REPL receives a user message

Each hook is a shell command. We pipe a JSON event to its stdin and read its
stdout + exit code:

    PreToolUse / UserPromptSubmit:
        - exit code == 0  AND empty/non-JSON stdout -> proceed unchanged
        - exit code == 0  AND stdout is JSON with `tool_input` / `prompt`
                                              -> proceed using overridden value
        - exit code != 0  -> BLOCK; tool/prompt is rejected with hook stderr
                            text fed back to the model (PreToolUse) or shown
                            to the user (UserPromptSubmit)
        - timeout (30s)   -> treated as block

    PostToolUse:
        - exit code is logged but never blocks (the tool already ran)
        - stdout is currently ignored

Matchers:
    - "*" matches any tool/event
    - exact tool name (e.g. "bash")
    - comma-separated list ("bash,write_file")
The matcher field is unused for UserPromptSubmit; "*" is conventional.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any


HOOK_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class HookSpec:
    matcher: str
    command: str

    def matches(self, name: str) -> bool:
        m = self.matcher.strip()
        if m in ("", "*"):
            return True
        for token in m.split(","):
            if token.strip() == name:
                return True
        return False


@dataclass
class HookOutcome:
    """Result of running all hooks for one event."""

    blocked: bool = False
    block_reason: str = ""
    overrides: dict[str, Any] = field(default_factory=dict)
    # Logs from each hook that ran (for the telemetry panel / debug output).
    log: list[str] = field(default_factory=list)


class HookRunner:
    """Reads hook config from a settings dict and dispatches events.

    Constructed once per AgentLoop. Hooks are referenced by key, e.g.
        runner.fire("PreToolUse", {"tool_name": "bash", "tool_input": {...}})
    """

    def __init__(self, hooks_config: dict[str, Any] | None) -> None:
        self._hooks: dict[str, list[HookSpec]] = {}
        for event, specs in (hooks_config or {}).items():
            cleaned: list[HookSpec] = []
            for entry in specs or []:
                if not isinstance(entry, dict):
                    continue
                cmd = entry.get("command")
                if not isinstance(cmd, str) or not cmd.strip():
                    continue
                cleaned.append(HookSpec(matcher=str(entry.get("matcher", "*")), command=cmd))
            if cleaned:
                self._hooks[event] = cleaned

    def has_hooks(self, event: str) -> bool:
        return bool(self._hooks.get(event))

    async def fire(self, event: str, payload: dict[str, Any]) -> HookOutcome:
        """Run all hooks matching `event` against `payload`.

        For PreToolUse / UserPromptSubmit, blocking + override semantics are
        applied. For PostToolUse, results are recorded but never block.
        """
        outcome = HookOutcome()
        specs = self._hooks.get(event, [])
        if not specs:
            return outcome

        match_target = payload.get("tool_name") or payload.get("event") or ""
        is_post = event == "PostToolUse"

        for spec in specs:
            if not spec.matches(match_target):
                continue
            try:
                proc = await asyncio.to_thread(self._run_one, spec.command, payload)
            except Exception as exc:
                outcome.log.append(f"[hook error] {spec.command!r}: {exc}")
                if not is_post:
                    outcome.blocked = True
                    outcome.block_reason = f"hook crashed: {exc}"
                    return outcome
                continue

            stdout, stderr, returncode = proc

            if not is_post:
                if returncode != 0:
                    outcome.blocked = True
                    outcome.block_reason = (stderr or stdout or f"hook exited {returncode}").strip()
                    outcome.log.append(f"[hook block] {spec.command!r} -> exit {returncode}")
                    return outcome

                # Optional override via stdout JSON: {"tool_input": {...}} or
                # {"prompt": "..."}. Anything else (including empty / non-JSON)
                # means "proceed unchanged".
                stdout_stripped = stdout.strip()
                if stdout_stripped:
                    try:
                        parsed = json.loads(stdout_stripped)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, dict):
                        for key in ("tool_input", "prompt"):
                            if key in parsed:
                                outcome.overrides[key] = parsed[key]
                outcome.log.append(f"[hook ok] {spec.command!r}")
            else:
                outcome.log.append(f"[hook post] {spec.command!r} -> exit {returncode}")

        return outcome

    @staticmethod
    def _run_one(command: str, payload: dict[str, Any]) -> tuple[str, str, int]:
        """Execute one hook command, piping JSON payload to stdin.

        Runs through the shell so command strings can use pipes / && / etc.
        """
        # On Windows shell=True invokes cmd.exe; on POSIX it uses /bin/sh.
        # Hook authors who need PowerShell can write `powershell -Command "..."`.
        proc = subprocess.run(
            command,
            shell=True,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=HOOK_TIMEOUT_SECONDS,
        )
        return proc.stdout, proc.stderr, proc.returncode
