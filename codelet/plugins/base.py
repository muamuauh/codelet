"""Plugin interface + the context a plugin registers its contributions into.

A plugin is any object with a `name` and a `setup(ctx)` method. In `setup` it
calls methods on the `PluginContext` to contribute:

  - `register_tool(tool)`          -- add a Tool (or override a core one by name)
  - `add_system_prompt_section(s)` -- extra text appended to the system prompt
  - `on_user_prompt(fn)`           -- fn(text) -> text, transforms each prompt
  - `wrap_tool(fn)`                -- async middleware around tool execution:
        async def fn(name, tool_input, call_next) -> ToolResult
  - `ctx.config`                   -- this plugin's dict from settings.json

Everything is optional; a plugin uses only what it needs. See
docs/plugin-architecture.md for worked sandbox / RAG examples.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from ..tools.base import Tool, ToolRegistry, ToolResult

PromptMiddleware = Callable[[str], str]
ToolMiddleware = Callable[[str, dict, "Callable[[], Awaitable[ToolResult]]"], Awaitable[ToolResult]]


@runtime_checkable
class Plugin(Protocol):
    name: str

    def setup(self, ctx: "PluginContext") -> None: ...


class PluginContext:
    """Handed to each plugin's `setup`; collects its contributions."""

    def __init__(self, registry: ToolRegistry, config: dict[str, Any] | None = None) -> None:
        self._registry = registry
        self.config: dict[str, Any] = config or {}
        self.prompt_sections: list[str] = []
        self.prompt_middleware: list[PromptMiddleware] = []
        self.tool_middleware: list[ToolMiddleware] = []
        self.commands: dict[str, Callable[[str], str]] = {}

    def register_tool(self, tool: Tool) -> None:
        """Add a tool. Registering a name that already exists overrides it
        (how a sandbox plugin replaces the core `bash`/`write_file`)."""
        self._registry.unregister(tool.name)
        self._registry.register(tool)

    def add_system_prompt_section(self, text: str) -> None:
        if text and text.strip():
            self.prompt_sections.append(text.strip())

    def on_user_prompt(self, fn: PromptMiddleware) -> None:
        self.prompt_middleware.append(fn)

    def wrap_tool(self, fn: ToolMiddleware) -> None:
        self.tool_middleware.append(fn)

    def register_command(self, name: str, fn: "Callable[[str], str]") -> None:
        """A slash command `/name args` -> a status string shown to the user
        (not sent to the model). Used e.g. by RAG for `/rag` index refresh."""
        self.commands[name.lstrip("/")] = fn
