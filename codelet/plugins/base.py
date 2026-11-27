"""Plugin interface + reversible `PluginContext` / `PluginHandle` lifecycle.

A plugin is any object with a `name` and a `setup(ctx)` method (plus optional
`requires: tuple[str, ...]` and `conflicts: tuple[str, ...]` declarations).
In `setup` it calls methods on `PluginContext` to contribute:

  - `register_tool(tool)`          -- add a Tool (or override a core one by name;
                                      automatically restored on dispose)
  - `register_skill(...)`          -- bundle a Skill that hot-plugs with the plugin
  - `register_skills_dir(dir)`     -- load all `*.md` / `<dir>/SKILL.md` in a dir
  - `register_command(name, fn)`   -- `/name args` -> status string
  - `register_panel(id, title, fn)`-- dynamic UI panel rendered in the Web Inspector
  - `provide(name, obj)` / `service(name)` -- share & consume cross-plugin services
  - `add_system_prompt_section(s)` -- extra text appended to the system prompt
  - `on_user_prompt(fn)`           -- fn(text) -> text, transforms each prompt
  - `wrap_tool(fn)`                -- async middleware around tool execution
  - `on_turn(fn)`                  -- fn(text) -> TurnPolicy | None
  - `on_dispose(fn)` / `effect(fn)`-- reversible cleanup hooks run on unmount
  - `ctx.config` / `ctx.host`      -- plugin config dict and live AgentLoop
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
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


@dataclass
class PluginPanel:
    """Dynamic UI panel registered by a plugin for the Web Inspector drawer.

    `render()` returns a JSON-serializable dict (either `{"markdown": "..."}`,
    `{"sections": [{"title": ..., "items": [{"label": ..., "value": ..., "badge": ...}]}]}`,
    or plain text/key-values) rendered generically by the Web UI without hardcoding
    plugin-specific HTML.
    """
    id: str
    title: str
    render: Callable[[], dict[str, Any]]
    plugin: str = ""
    description: str = ""


PromptMiddleware = Callable[[str], str]
TurnPolicyFn = Callable[[str], "TurnPolicy | None | Awaitable[TurnPolicy | None]"]
ToolMiddleware = Callable[[str, dict, "Callable[[], Awaitable[ToolResult]]"], Awaitable[ToolResult]]
Disposer = Callable[[], Any]


@runtime_checkable
class Plugin(Protocol):
    name: str

    def setup(self, ctx: "PluginContext") -> None: ...


class PluginContext:
    """Handed to each plugin's `setup`; tracks every contribution for clean disposal."""

    def __init__(
        self,
        registry: ToolRegistry,
        config: dict[str, Any] | None = None,
        host: Any = None,
        *,
        plugin_name: str = "",
        services: dict[str, Any] | None = None,
    ) -> None:
        self._registry = registry
        self.plugin_name = plugin_name
        self.config: dict[str, Any] = config or {}
        self.host: Any = host
        self._services: dict[str, Any] = services if services is not None else {}

        self.added_tools: list[str] = []
        self.overridden_tools: dict[str, Tool] = {}
        self.provided_services: list[str] = []
        self.prompt_sections: list[str] = []
        self.prompt_middleware: list[PromptMiddleware] = []
        self.tool_middleware: list[ToolMiddleware] = []
        self.commands: dict[str, Callable[[str], str]] = {}
        self.turn_policies: list[TurnPolicyFn] = []
        self.skills: list[Skill] = []
        self.panels: list[PluginPanel] = []
        self.disposers: list[Disposer] = []

    def register_tool(self, tool: Tool) -> None:
        """Add a tool (or override an existing one; original is restored on dispose)."""
        prev = self._registry.get(tool.name)
        if prev is not None and tool.name not in self.overridden_tools and tool.name not in self.added_tools:
            self.overridden_tools[tool.name] = prev
        self._registry.unregister(tool.name)
        self._registry.register(tool)
        if tool.name not in self.added_tools:
            self.added_tools.append(tool.name)

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
            sk = parse_skill_file(md, scope="plugin", plugin=plugin_name or self.plugin_name or None)
            if sk is not None:
                self.skills.append(sk)
                added.append(sk.name)
        return added

    def register_panel(
        self,
        panel_id: str,
        title: str,
        render: Callable[[], dict[str, Any]],
        *,
        description: str = "",
    ) -> None:
        """Register a dynamic Inspector panel for the Web workbench."""
        pid = panel_id.strip()
        if not pid:
            raise ValueError("panel_id must not be empty")
        self.panels.append(
            PluginPanel(
                id=pid,
                title=title.strip() or pid,
                render=render,
                plugin=self.plugin_name,
                description=description.strip(),
            )
        )

    def provide(self, name: str, obj: Any) -> None:
        """Expose a service object to downstream plugins (removed on dispose)."""
        self._services[name] = obj
        if name not in self.provided_services:
            self.provided_services.append(name)

    def service(self, name: str, default: Any = None) -> Any:
        """Retrieve a service provided by an upstream plugin."""
        return self._services.get(name, default)

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
        """A slash command `/name args` -> a status string shown to the user."""
        self.commands[name.lstrip("/")] = fn

    def on_dispose(self, fn: Disposer) -> None:
        """Register a cleanup callback invoked automatically when this plugin unmounts."""
        self.disposers.append(fn)

    def effect(self, setup_fn: Callable[[], Disposer | None]) -> None:
        """Cordis-style reversible effect: run `setup_fn()` now and record its
        returned cleanup callable (if any) to run on dispose."""
        cleanup = setup_fn()
        if callable(cleanup):
            self.disposers.append(cleanup)


@dataclass
class PluginHandle:
    """Live ownership record for one mounted plugin; `dispose()` unwinds everything."""

    name: str
    requires: tuple[str, ...] = ()
    tools: list[str] = field(default_factory=list)
    overridden_tools: dict[str, Tool] = field(default_factory=dict)
    skills: list[Skill] = field(default_factory=list)
    commands: dict[str, Callable[[str], str]] = field(default_factory=dict)
    prompt_sections: list[str] = field(default_factory=list)
    prompt_middleware: list[PromptMiddleware] = field(default_factory=list)
    tool_middleware: list[ToolMiddleware] = field(default_factory=list)
    turn_policies: list[TurnPolicyFn] = field(default_factory=list)
    panels: list[PluginPanel] = field(default_factory=list)
    provided_services: list[str] = field(default_factory=list)
    disposers: list[Disposer] = field(default_factory=list)
    active: bool = True

    def dispose(self, registry: ToolRegistry, services: dict[str, Any] | None = None) -> None:
        """Unwind all tools, services, and custom disposers cleanly (idempotent)."""
        if not self.active:
            return
        self.active = False

        # Run custom disposers in reverse (LIFO) order; isolate individual failures.
        for fn in reversed(self.disposers):
            try:
                fn()
            except Exception as exc:
                print(f"[plugin] dispose hook for '{self.name}' failed: {exc}", file=sys.stderr)
        self.disposers.clear()

        # Remove registered tools and restore any overridden core/previous tools.
        for tname in reversed(self.tools):
            registry.unregister(tname)
            prev = self.overridden_tools.get(tname)
            if prev is not None:
                registry.register(prev)

        # Remove shared services provided by this plugin.
        if services is not None:
            for sname in self.provided_services:
                services.pop(sname, None)
