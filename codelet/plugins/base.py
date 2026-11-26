"""Plugin interface + the context a plugin registers its contributions into.

A plugin is any object with a `name` and a `setup(ctx)` method. In `setup` it
calls methods on the `PluginContext` to contribute:

  - `register_tool(tool)`          -- add a Tool (or override a core one by name)
  - `add_system_prompt_section(s)` -- extra text appended to the system prompt
  - `on_user_prompt(fn)`           -- fn(text) -> text, transforms each prompt
  - `wrap_tool(fn)`                -- async middleware around tool execution:
        async def fn(name, tool_input, call_next) -> ToolResult
  - `on_turn(fn)`                  -- fn(text) -> TurnPolicy | None (may be async),
        consulted once per user turn; the first policy returned applies
  - `ctx.config`                   -- this plugin's dict from settings.json
  - `ctx.host`                     -- the live AgentLoop (or None), for plugins
        that hot-activate tools mid-session (see the `evolve` self-evolution
        plugin, which reaches back through the host to register a just-authored
        tool into the running registry).

Everything is optional; a plugin uses only what it needs. See
docs/plugin-architecture.md for worked sandbox / RAG examples.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from ..skills.loader import Skill, iter_skill_files, parse_skill_file
from ..tools.base import Tool, ToolRegistry, ToolResult

@dataclass
class TurnPolicy:
    """How one user turn should run. `read_only`: only tools whose
    `is_read_only(params)` is true may run this turn. `note` is appended to the
    user message so the model knows why."""
    label: str
    read_only: bool = False
    note: str = ""


PromptMiddleware = Callable[[str], str]
TurnPolicyFn = Callable[[str], "TurnPolicy | None | Awaitable[TurnPolicy | None]"]
ToolMiddleware = Callable[[str, dict, "Callable[[], Awaitable[ToolResult]]"], Awaitable[ToolResult]]


@runtime_checkable
class Plugin(Protocol):
    name: str

    def setup(self, ctx: "PluginContext") -> None: ...


class PluginContext:
    """Handed to each plugin's `setup`; collects its contributions."""

    def __init__(
        self,
        registry: ToolRegistry,
        config: dict[str, Any] | None = None,
        host: Any = None,
    ) -> None:
        self._registry = registry
        self.config: dict[str, Any] = config or {}
        # The AgentLoop this plugin is being applied to (None in some tests /
        # for the listing-only registry). Plugins that need to mutate the live
        # session -- e.g. self-evolution -- use it; most plugins ignore it.
        self.host: Any = host
        self.prompt_sections: list[str] = []
        self.prompt_middleware: list[PromptMiddleware] = []
        self.tool_middleware: list[ToolMiddleware] = []
        self.commands: dict[str, Callable[[str], str]] = {}
        self.turn_policies: list[TurnPolicyFn] = []
        self.skills: list[Skill] = []

    def register_tool(self, tool: Tool) -> None:
        """Add a tool. Registering a name that already exists overrides it, so a
        plugin can either add a new capability or replace a core tool by name."""
        self._registry.unregister(tool.name)
        self._registry.register(tool)

    def register_skill(
        self,
        skill: Skill | None = None,
        *,
        name: str = "",
        description: str = "",
        body: str = "",
        category: str = "",
        triggers: tuple[str, ...] = (),
        allowed_tools: tuple[str, ...] | None = None,
    ) -> None:
        """Bundle a Skill with this plugin so it hot-plugs in/out with the plugin."""
        if skill is None:
            if not name or not description:
                raise ValueError("register_skill requires a Skill instance or name + description")
            skill = Skill(
                name=name.strip(),
                description=description.strip(),
                body=body.strip(),
                triggers=triggers,
                allowed_tools=allowed_tools,
                category=category.strip(),
                scope="plugin",
            )
        self.skills.append(skill)

    def register_skills_dir(self, directory: str | Path, *, plugin_name: str | None = None) -> list[str]:
        """Load every flat `*.md` and `<dir>/SKILL.md` inside `directory`."""
        d = Path(directory)
        added: list[str] = []
        for md in iter_skill_files(d):
            sk = parse_skill_file(md, scope="plugin", plugin=plugin_name)
            if sk is not None:
                self.skills.append(sk)
                added.append(sk.name)
        return added

    def add_system_prompt_section(self, text: str) -> None:
        if text and text.strip():
            self.prompt_sections.append(text.strip())

    def on_user_prompt(self, fn: PromptMiddleware) -> None:
        self.prompt_middleware.append(fn)

    def wrap_tool(self, fn: ToolMiddleware) -> None:
        self.tool_middleware.append(fn)

    def on_turn(self, fn: TurnPolicyFn) -> None:
        """Decide how each user turn runs (e.g. the intent router's read-only turns)."""
        self.turn_policies.append(fn)

    def register_command(self, name: str, fn: "Callable[[str], str]") -> None:
        """A slash command `/name args` -> a status string shown to the user
        (not sent to the model). Used e.g. by RAG for `/rag` index refresh."""
        self.commands[name.lstrip("/")] = fn
