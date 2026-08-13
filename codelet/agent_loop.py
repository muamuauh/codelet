"""Agent Loop -- async dispatch, hooks, compaction, telemetry.

Flow per user turn:
  1. UserPromptSubmit hooks fire (may rewrite or block the prompt)
  2. Append user message
  3. Loop:
       a. await LLM (sync client wrapped in to_thread); record telemetry
       b. Render text + tool_use blocks
       c. For each tool_use, in parallel:
            - PreToolUse hooks fire (may rewrite input or block)
            - permission gate
            - tool.aexecute
            - PostToolUse hooks fire (best-effort, never block)
       d. Append assistant message + tool_results
       e. Compact context if past token threshold
       f. Stop when response carries no tool_use OR max_turns reached

Public API:
  - `run(user_message)` is the sync entry point used by the REPL; it wraps
    `run_async` via asyncio.run, so callers don't need to be async-aware.
  - `run_async(user_message)` is the canonical async coroutine.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Callable

from rich.console import Console
from rich.syntax import Syntax

from .config import Config, PermissionMode
from .context import ConversationContext
from .hooks.runner import HookRunner
from .llm.base import LLMClient, LLMResponse, ToolCall
from .llm.factory import build_client
from .permissions import PermissionGate
from .skills.loader import SkillIndex, load_skills
from .system_prompt import build_system_prompt
from .telemetry import Telemetry
from .tools.base import ToolRegistry, ToolResult
from .tools.skill_tool import SkillTool
from .tools.task_tool import TaskTool
from .tools.todo_write import TodoStore, TodoWriteTool

# Tools that should pop a diff preview + y/n confirm in ASK mode.
_DIFF_CONFIRM_TOOLS = {"write_file", "edit_file"}

# Playful status words shown while waiting for the first streamed token.
_SPINNER_WORDS = (
    "Pondering", "Brewing", "Conjuring", "Noodling", "Percolating",
    "Ruminating", "Synthesizing", "Tinkering", "Musing", "Computing",
    "Scheming", "Wrangling", "Cogitating", "Marinating", "Spelunking",
)


class PromptBlocked(RuntimeError):
    """Raised when a UserPromptSubmit hook rejects the input."""


class AgentLoop:
    def __init__(
        self,
        config: Config | None = None,
        registry: ToolRegistry | None = None,
        client: LLMClient | None = None,
        console: Console | None = None,
        skill_index: SkillIndex | None = None,
        todo_store: TodoStore | None = None,
        allowed_tools: tuple[str, ...] | None = None,
        hook_runner: HookRunner | None = None,
        telemetry: Telemetry | None = None,
        confirm_callback: "Callable[[str, str], bool] | None" = None,
        _is_subagent: bool = False,
    ) -> None:
        self.config = config or Config()
        self.registry = registry or ToolRegistry.default()
        self.permission_gate = PermissionGate(self.config)
        self.context = ConversationContext(config=self.config)
        self.client = client or build_client(self.config)
        self.console = console or Console()

        # Stream assistant text live (+ spinner). Off for subagents -- their
        # output isn't shown to the user, and concurrent subagents would
        # interleave on the console. Also skipped at call time for non-TTY output.
        self._stream = (not _is_subagent) and getattr(self.config, "stream", True)

        # Skills: project-local + user-global. Loaded once; SubAgentSession
        # passes the same instance into child loops by reference (cheap copy).
        self.skill_index = skill_index if skill_index is not None else load_skills()

        # Todo store: parent owns its own; subagents get a fresh one (so their
        # bookkeeping doesn't pollute the parent's panel).
        self.todo_store = todo_store or TodoStore()

        # Hooks: parent reads from Config.hooks; subagents inherit the same
        # runner instance (passed in by the SubAgent runner).
        self.hooks = hook_runner if hook_runner is not None else HookRunner(self.config.hooks)

        # Telemetry: parent owns one; subagents get their own (token cost is
        # tracked separately so the panel can break out subagent share later).
        self.telemetry = telemetry if telemetry is not None else Telemetry()
        self.telemetry.update_pricing(self.config.pricing_overrides)

        # Destructive-write confirmation. Default uses Console.input; tests
        # inject a deterministic stub. Subagents inherit `confirm_callback=None`
        # which means "auto-approve" -- we never want a subagent to block on
        # interactive input the user can't see.
        self._confirm_callback: Callable[[str, str], bool] | None = (
            confirm_callback if confirm_callback is not None
            else (None if _is_subagent else self._default_confirm)
        )

        # Honor an explicit `allowed_tools` whitelist by stripping anything
        # outside it BEFORE wiring dynamic tools. Subagents use this to enforce
        # the spawn-time tool subset.
        if allowed_tools is not None:
            allowed = set(allowed_tools)
            for existing in list(self.registry.names()):
                if existing not in allowed:
                    self.registry.unregister(existing)

        # Auto-register Task/Skill/TodoWrite (bound to *this* loop) unless they
        # are absent from the allowed_tools whitelist. Each is bound to per-loop
        # state so subagents get their own TodoStore and TaskTool sees the
        # correct depth.
        self._wire_dynamic_tools(allowed_tools)

        # SubAgents skip CLAUDE.md: their instructions come from the spawn prompt.
        # The runner overwrites the system prompt afterwards anyway, but we
        # set a sensible default here for the non-subagent case.
        if not _is_subagent:
            system_prompt = build_system_prompt(
                self.registry,
                permission_mode=self.config.permission_mode.value,
                skill_index=self.skill_index,
            )
            self.context.set_system_prompt(system_prompt)

    # ---------- public API ----------

    def run(self, user_message: str) -> str:
        """Synchronous wrapper for REPL use."""
        return asyncio.run(self.run_async(user_message))

    async def run_async(self, user_message: str) -> str:
        # Mark a fresh user-turn boundary so telemetry can break this turn out
        # from cumulative session totals.
        self.telemetry.begin_user_turn()

        # UserPromptSubmit hooks: can rewrite the prompt or block it outright.
        if self.hooks.has_hooks("UserPromptSubmit"):
            outcome = await self.hooks.fire("UserPromptSubmit", {
                "event": "UserPromptSubmit",
                "prompt": user_message,
            })
            if outcome.blocked:
                raise PromptBlocked(outcome.block_reason or "UserPromptSubmit hook blocked the prompt")
            if "prompt" in outcome.overrides:
                user_message = str(outcome.overrides["prompt"])

        self.context.add_user_message(user_message)
        final_text = ""

        for _turn in range(self.config.max_turns):
            response, streamed = await self._call_llm()
            self._render_response(response, streamed)

            if response.text_blocks:
                final_text = "\n".join(response.text_blocks)

            if not response.tool_calls:
                if response.raw_content:
                    self.context.add_assistant_message(response.raw_content)
                break

            self.context.add_assistant_message(response.raw_content)
            tool_results = await self._dispatch_parallel(response.tool_calls)
            self.context.add_tool_results(tool_results)

            # Opportunistic compaction: never mid-tool-execution -- only after
            # a full tool round-trip is appended, so we don't accidentally
            # decapitate an in-flight tool_use/tool_result pair.
            try:
                await self.context.compact_if_needed(self.client)
            except Exception as exc:
                # A failing summarizer must NOT kill the agent. Log and move on.
                self.console.print(f"[dim yellow][compaction skipped: {exc}][/dim yellow]")
        else:
            if not final_text:
                final_text = "(max turns reached without a final response)"

        return final_text

    # ---------- internals ----------

    def _wire_dynamic_tools(self, allowed_tools: tuple[str, ...] | None) -> None:
        """Bind Task/Skill/TodoWrite to *this* loop.

        If a Task/TodoWriteTool was inherited from a parent registry, replace
        it -- those instances captured the parent's loop and would report the
        wrong depth / write to the wrong todo store. Skipped entirely if the
        name is excluded by `allowed_tools`.
        """
        allow = set(allowed_tools) if allowed_tools is not None else None

        def maybe(name: str, factory) -> None:
            if allow is not None and name not in allow:
                return
            # Replace any inherited binding so this loop owns the tool.
            self.registry.unregister(name)
            self.registry.register(factory())

        maybe("task", lambda: TaskTool(self))
        if self.skill_index.names():
            # Only expose Skill when at least one skill is loaded -- otherwise
            # we'd advertise an empty capability and waste tokens.
            maybe("skill", lambda: SkillTool(self.skill_index))
        maybe("todo_write", lambda: TodoWriteTool(self.todo_store))

    async def _call_llm(self) -> tuple[LLMResponse, bool]:
        """Run one LLM call. Returns (response, streamed_text).

        When `streamed_text` is True, the assistant's text was already printed
        live, so the caller must not re-print it.
        """
        # The SDK call is sync; offload to a worker thread so we don't block the
        # event loop (and so the spinner / stream consumer can run concurrently).
        if not self._stream or not self.console.is_terminal:
            response = await asyncio.to_thread(self._chat_blocking)
            self._record_usage(response)
            return response, False

        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[str | None]" = asyncio.Queue()

        def on_text(delta: str) -> None:
            # Runs in the worker thread -- hop back onto the loop thread safely.
            loop.call_soon_threadsafe(queue.put_nowait, delta)

        async def call() -> LLMResponse:
            try:
                return await asyncio.to_thread(self._chat_blocking, on_text)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)  # end sentinel

        task = asyncio.create_task(call())

        first = await self._spin_until_first_token(queue)
        printed = False
        if first is not None:
            self._print_stream_text(first)
            printed = True
            while True:
                item = await queue.get()
                if item is None:
                    break
                self._print_stream_text(item)
        if printed:
            self.console.print()  # terminate the streamed line

        response = await task
        self._record_usage(response)
        return response, printed

    def _chat_blocking(self, on_text: "Callable[[str], None] | None" = None) -> LLMResponse:
        return self.client.chat(
            messages=self.context.get_api_messages(),
            system=self.context.system_prompt,
            tools=self.registry.api_schemas(),
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            on_text=on_text,
        )

    def _record_usage(self, response: LLMResponse) -> None:
        if response.usage:
            self.telemetry.record_chat(self.config.model, response.usage)

    async def _spin_until_first_token(self, queue: "asyncio.Queue[str | None]") -> str | None:
        """Show a spinner with a rotating word until the first queue item lands.
        Returns that item (a text delta, or None if the turn produced no text)."""
        start = time.monotonic()
        word = random.choice(_SPINNER_WORDS)
        with self.console.status(f"[dim]{word}…[/dim]", spinner="dots") as status:
            while True:
                try:
                    return await asyncio.wait_for(queue.get(), timeout=2.5)
                except asyncio.TimeoutError:
                    word = random.choice(_SPINNER_WORDS)
                    elapsed = time.monotonic() - start
                    status.update(f"[dim]{word}… ({elapsed:.0f}s)[/dim]")

    def _print_stream_text(self, text: str) -> None:
        # Model text is data, not Rich markup -- never let a stray "[" be parsed
        # as a tag, and don't auto-highlight numbers/paths.
        self.console.print(text, end="", markup=False, highlight=False, soft_wrap=True)

    def _render_response(self, response: LLMResponse, streamed: bool = False) -> None:
        for block in response.raw_content:
            if block["type"] == "text":
                if streamed:
                    continue  # already printed live during streaming
                self.console.print(block["text"], end="")
            elif block["type"] == "tool_use":
                preview = str(block.get("input", ""))
                if len(preview) > 120:
                    preview = preview[:120] + "..."
                self.console.print(f"\n[dim cyan][tool: {block['name']}][/dim cyan] {preview}")

    async def _dispatch_parallel(self, tool_calls: list[ToolCall]) -> list[dict[str, Any]]:
        """Run all tool_use blocks concurrently; emit results in tool_use order.

        IMPORTANT: Anthropic requires `tool_result` blocks to appear in the
        same order as the originating `tool_use` blocks. We collect by
        tool_use_id and re-emit in the original order, so completion order
        cannot leak through to the API.
        """
        if not tool_calls:
            return []

        # Single-call shortcut: no need to spin up gather machinery.
        if len(tool_calls) == 1:
            return [await self._dispatch_one(tool_calls[0])]

        coros = [self._dispatch_one(call) for call in tool_calls]
        # return_exceptions=True isolates sibling failures: one tool blowing up
        # never cancels the others. Each exception is converted to a tool_result.
        completed = await asyncio.gather(*coros, return_exceptions=True)

        by_id: dict[str, dict[str, Any]] = {}
        for call, item in zip(tool_calls, completed):
            if isinstance(item, BaseException):
                by_id[call.id] = self._error_result(call.id, f"Dispatcher error: {item}")
            else:
                by_id[call.id] = item

        # Order-preservation: walk tool_calls again, not `completed`.
        return [by_id[call.id] for call in tool_calls]

    async def _dispatch_one(self, call: ToolCall) -> dict[str, Any]:
        tool = self.registry.get(call.name)
        if tool is None:
            return self._error_result(call.id, f"Unknown tool '{call.name}'")

        # PreToolUse hooks: may rewrite tool input or block execution.
        tool_input: dict[str, Any] = dict(call.input or {})
        if self.hooks.has_hooks("PreToolUse"):
            outcome = await self.hooks.fire("PreToolUse", {
                "event": "PreToolUse",
                "tool_name": call.name,
                "tool_input": tool_input,
            })
            if outcome.blocked:
                msg = outcome.block_reason or "PreToolUse hook blocked execution"
                self.console.print(f"  [red]-> [hook block] {msg}[/red]")
                return self._error_result(call.id, f"PreToolUse blocked: {msg}")
            if "tool_input" in outcome.overrides and isinstance(outcome.overrides["tool_input"], dict):
                tool_input = outcome.overrides["tool_input"]

        denial = self.permission_gate.check(tool, tool_input)
        if denial is not None:
            self.console.print(f"  [red]-> {denial.output}[/red]")
            return self._error_result(call.id, denial.output)

        # Diff preview + confirmation for destructive writes in ASK mode. The
        # callback is None for subagents and for tests that explicitly opt out.
        if (
            self.config.permission_mode == PermissionMode.ASK
            and call.name in _DIFF_CONFIRM_TOOLS
            and self._confirm_callback is not None
        ):
            preview = tool.preview_diff(tool_input)
            if preview:
                if not self._confirm_callback(call.name, preview):
                    self.console.print("  [yellow]-> rejected by user[/yellow]")
                    return self._error_result(call.id, "User rejected the proposed change.")

        try:
            result: ToolResult = await tool.aexecute(tool_input)
        except Exception as exc:
            result = ToolResult(output=f"Tool raised: {exc}", is_error=True)

        preview = result.output if len(result.output) <= 300 else result.output[:300] + "..."
        status = "ERR" if result.is_error else "OK"
        color = "red" if result.is_error else "green"
        self.console.print(f"  [{color}]-> [{status}][/{color}] {preview}")

        # PostToolUse: best-effort logging, never blocks the result.
        if self.hooks.has_hooks("PostToolUse"):
            await self.hooks.fire("PostToolUse", {
                "event": "PostToolUse",
                "tool_name": call.name,
                "tool_input": tool_input,
                "tool_output": result.output,
                "is_error": result.is_error,
            })

        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": result.output,
            "is_error": result.is_error,
        }

    @staticmethod
    def _error_result(tool_use_id: str, message: str) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": message,
            "is_error": True,
        }

    def _default_confirm(self, tool_name: str, diff: str) -> bool:
        """Render a colored diff via Rich and prompt for y/n.

        Used only when the loop is interactive (no `confirm_callback` override).
        """
        self.console.print(f"\n[bold]proposed change via [cyan]{tool_name}[/cyan]:[/bold]")
        self.console.print(Syntax(diff, "diff", theme="ansi_dark", line_numbers=False))
        try:
            answer = self.console.input("[bold]apply this change?[/bold] [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")
