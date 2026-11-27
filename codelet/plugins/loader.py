"""Discover plugins, resolve `requires` dependencies, mount/unmount with reversible handles.

Discovery (mirrors how skills/commands load):
  1. Python entry points in group `codelet.plugins` (pip-installable plugins).
  2. `./.codelet/plugins/` (project) and `~/.codelet/plugins/` (user):
     - single `*.py` file exposing module-level `PLUGIN`
     - directory bundle `<name>/` containing `plugin.py` and/or `skills/*.md`

The `plugins` block from settings.json curates them:
    {"plugins": {"enabled": ["cockpit"], "config": {"cockpit": {...}}}}
Plugins may optionally declare `requires = ("other_plugin",)` and
`conflicts = ("rival_plugin",)`. `apply_plugins` topologically sorts enabled
plugins and tracks a reversible `PluginHandle` per plugin so `unmount()` /
`mount_one()` hot-plug in place without rebuilding the AgentLoop.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from ..skills.loader import Skill, iter_skill_files
from ..tools.base import ToolRegistry
from .base import (
    Plugin,
    PluginContext,
    PluginHandle,
    PluginPanel,
    PromptMiddleware,
    ToolMiddleware,
    TurnPolicyFn,
)

# Built-in plugins ship with codelet but load ONLY when named in
# settings.json plugins.enabled (never auto-discovered -- a sandbox silently
# overriding bash would be nasty).
_BUILTIN = {
    "cockpit": "codelet.plugins.builtin.cockpit",
    "sandbox": "codelet.plugins.builtin.sandbox",
    "rag": "codelet.plugins.builtin.rag",
    "evolve": "codelet.plugins.builtin.evolve",
    "memory": "codelet.plugins.builtin.memory",
    "router": "codelet.plugins.builtin.router",
    "mcp": "codelet.plugins.builtin.mcp",
}

BUILTIN_DESCRIPTIONS: dict[str, str] = {
    "cockpit": "Real-time context gauge, speed/cache telemetry & spend dashboard (/cockpit).",
    "sandbox": "Run shell commands inside an isolated one-shot Docker container.",
    "rag": "Local BM25 full-text search across workspace files (/rag, search_docs).",
    "evolve": "Self-evolution & Creator mode: author tools and plugin packs live (/evolve).",
    "memory": "Cross-session typed markdown memory (/memory).",
    "router": "Intent router: make read-only Q&A/planning turns unable to edit files.",
    "mcp": "Bridge external MCP servers (stdio / HTTP) into native codelet tools (/mcp).",
}


@dataclass
class AppliedPlugins:
    names: list[str] = field(default_factory=list)
    prompt_sections: list[str] = field(default_factory=list)
    prompt_middleware: list[PromptMiddleware] = field(default_factory=list)
    tool_middleware: list[ToolMiddleware] = field(default_factory=list)
    commands: dict[str, Callable[[str], str]] = field(default_factory=dict)
    turn_policies: list[TurnPolicyFn] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    panels: dict[str, PluginPanel] = field(default_factory=dict)
    tool_origins: dict[str, str] = field(default_factory=dict)
    skill_origins: dict[str, str] = field(default_factory=dict)
    details: dict[str, dict[str, Any]] = field(default_factory=dict)
    handles: dict[str, PluginHandle] = field(default_factory=dict)
    services: dict[str, Any] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)

    def rebuild_aggregates(self) -> None:
        """Recompute flat contribution lists/maps from active `handles` in mount order."""
        self.names = []
        self.prompt_sections = []
        self.prompt_middleware = []
        self.tool_middleware = []
        self.commands = {}
        self.turn_policies = []
        self.skills = []
        self.panels = {}
        self.tool_origins = {}
        self.skill_origins = {}
        self.details = {}

        for name, h in self.handles.items():
            if not h.active:
                continue
            self.names.append(name)
            self.prompt_sections.extend(h.prompt_sections)
            self.prompt_middleware.extend(h.prompt_middleware)
            self.tool_middleware.extend(h.tool_middleware)
            self.commands.update(h.commands)
            self.turn_policies.extend(h.turn_policies)
            self.skills.extend(h.skills)
            for p in h.panels:
                self.panels[p.id] = p
            for tname in h.tools:
                self.tool_origins[tname] = name
            for sk in h.skills:
                self.skill_origins[sk.name] = name
            self.details[name] = {
                "tools": list(h.tools),
                "skills": [sk.name for sk in h.skills],
                "commands": sorted(h.commands.keys()),
                "panels": [{"id": p.id, "title": p.title} for p in h.panels],
                "requires": list(h.requires),
                "has_turn_policy": bool(h.turn_policies),
                "has_middleware": bool(h.prompt_middleware or h.tool_middleware),
            }

    def mount_instance(
        self,
        name: str,
        plugin: Plugin,
        registry: ToolRegistry,
        config: dict[str, Any] | None = None,
        *,
        host: Any = None,
    ) -> PluginHandle | None:
        """Mount one plugin into `registry` and record its reversible `PluginHandle`."""
        if name in self.handles and self.handles[name].active:
            return self.handles[name]

        requires = tuple(str(r) for r in (getattr(plugin, "requires", ()) or ()))
        missing = [r for r in requires if r not in self.handles or not self.handles[r].active]
        if missing:
            reason = f"missing required plugin(s): {', '.join(missing)}"
            self.skipped[name] = reason
            _warn(f"plugin '{name}' skipped: {reason}")
            return None

        conflicts = tuple(str(c) for c in (getattr(plugin, "conflicts", ()) or ()))
        clashing = [c for c in conflicts if c in self.handles and self.handles[c].active]
        if clashing:
            reason = f"conflicts with active plugin(s): {', '.join(clashing)}"
            self.skipped[name] = reason
            _warn(f"plugin '{name}' skipped: {reason}")
            return None

        ctx = PluginContext(
            registry,
            config or {},
            host=host,
            plugin_name=name,
            services=self.services,
        )
        try:
            plugin.setup(ctx)
        except Exception as exc:
            # Roll back partial registrations if setup failed mid-way.
            partial = PluginHandle(
                name=name,
                tools=list(ctx.added_tools),
                overridden_tools=dict(ctx.overridden_tools),
                provided_services=list(ctx.provided_services),
                disposers=list(ctx.disposers),
            )
            partial.dispose(registry, self.services)
            self.skipped[name] = f"setup failed: {exc}"
            _warn(f"plugin '{name}' setup failed: {exc}")
            return None

        tagged_skills = [replace(sk, plugin=name, scope="plugin") for sk in ctx.skills]
        handle = PluginHandle(
            name=name,
            requires=requires,
            tools=list(ctx.added_tools),
            overridden_tools=dict(ctx.overridden_tools),
            skills=tagged_skills,
            commands=dict(ctx.commands),
            prompt_sections=list(ctx.prompt_sections),
            prompt_middleware=list(ctx.prompt_middleware),
            tool_middleware=list(ctx.tool_middleware),
            turn_policies=list(ctx.turn_policies),
            panels=list(ctx.panels),
            provided_services=list(ctx.provided_services),
            disposers=list(ctx.disposers),
        )
        self.skipped.pop(name, None)
        self.handles[name] = handle
        self.rebuild_aggregates()
        return handle

    def unmount(self, name: str, registry: ToolRegistry) -> list[str]:
        """Unmount `name` and cascade-unmount any active plugins that depend on it.

        Returns the list of unmounted plugin names (dependents first).
        """
        if name not in self.handles or not self.handles[name].active:
            return []

        unmounted: list[str] = []

        def _teardown(target: str) -> None:
            # Cascade-dispose active dependents before disposing their dependency.
            dependents = [
                other for other, h in list(self.handles.items())
                if h.active and target in h.requires and other != target
            ]
            for dep in dependents:
                _teardown(dep)
            h = self.handles.pop(target, None)
            if h is not None and h.active:
                h.dispose(registry, self.services)
                unmounted.append(target)

        _teardown(name)
        self.rebuild_aggregates()
        return unmounted

    def dispose_all(self, registry: ToolRegistry) -> None:
        for name in list(reversed(list(self.handles.keys()))):
            self.unmount(name, registry)


def _warn(msg: str) -> None:
    print(f"[plugin] {msg}", file=sys.stderr)


def _instantiate(obj: Any) -> Plugin | None:
    try:
        plugin = obj() if isinstance(obj, type) else obj
    except Exception as exc:
        _warn(f"could not instantiate {obj!r}: {exc}")
        return None
    if not hasattr(plugin, "setup"):
        _warn(f"ignoring {plugin!r}: no setup() method")
        return None
    return plugin


def _load_from_file(path: Path) -> Plugin | None:
    try:
        mod_name = f"_codelet_plugin_{path.stem}_{abs(hash(str(path.resolve()))) & 0xFFFF:x}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:
        _warn(f"failed to import {path}: {exc}")
        return None
    obj = getattr(mod, "PLUGIN", None)
    if obj is None:
        _warn(f"{path} has no module-level PLUGIN")
        return None
    return _instantiate(obj)


class _DirectoryBundlePlugin:
    """Wrapper for a directory-based plugin `.codelet/plugins/<name>/`.

    Supports both:
      - hybrid packs: `plugin.py` + optional `skills/*.md`
      - pure skill packs: `skills/*.md` with zero Python boilerplate
    """

    def __init__(self, name: str, inner: Plugin | None, skills_dir: Path | None, description: str = "") -> None:
        self.name = name
        self._inner = inner
        self._skills_dir = skills_dir
        self.requires = tuple(getattr(inner, "requires", ()) or ()) if inner is not None else ()
        self.conflicts = tuple(getattr(inner, "conflicts", ()) or ()) if inner is not None else ()
        self.description = description or (
            getattr(inner, "description", "") if inner is not None
            else f"Skill pack plugin ({name})"
        )

    def setup(self, ctx: PluginContext) -> None:
        if self._inner is not None:
            self._inner.setup(ctx)
        if self._skills_dir is not None and self._skills_dir.is_dir():
            ctx.register_skills_dir(self._skills_dir, plugin_name=self.name)


def _load_from_dir(directory: Path) -> Plugin | None:
    plugin_py = directory / "plugin.py"
    skills_dir = directory / "skills"
    skill_files = iter_skill_files(skills_dir) if skills_dir.is_dir() else []
    has_skills = bool(skill_files)
    inner = _load_from_file(plugin_py) if plugin_py.is_file() else None
    if inner is None and not has_skills:
        return None
    name = getattr(inner, "name", None) or directory.name
    if inner is not None and not has_skills:
        return inner
    desc = getattr(inner, "description", "") if inner is not None else ""
    if not desc and has_skills:
        count = len(skill_files)
        desc = f"Bundled skill pack ({count} skill{'s' if count != 1 else ''})."
    return _DirectoryBundlePlugin(name=name, inner=inner, skills_dir=skills_dir if has_skills else None, description=desc)


def discover_plugins(
    *, extra: list[Plugin] | None = None, plugin_dirs: list[Path] | None = None
) -> list[Plugin]:
    """All discovered plugins. `extra` (used by tests) is appended verbatim."""
    found: list[Plugin] = []

    # 1. entry points
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        group = (eps.select(group="codelet.plugins")
                 if hasattr(eps, "select") else eps.get("codelet.plugins", []))
        for ep in group:
            try:
                p = _instantiate(ep.load())
                if p is not None:
                    found.append(p)
            except Exception as exc:
                _warn(f"entry point {ep!r} failed: {exc}")
    except Exception:
        pass

    # 2. local files & directory bundle plugins (`*.py` and `<name>/`)
    dirs = plugin_dirs if plugin_dirs is not None else [
        Path.cwd() / ".codelet" / "plugins",
        Path.home() / ".codelet" / "plugins",
    ]
    for base in dirs:
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.name.startswith("_") or entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix == ".py":
                p = _load_from_file(entry)
                if p is not None:
                    found.append(p)
            elif entry.is_dir():
                p = _load_from_dir(entry)
                if p is not None:
                    found.append(p)

    if extra:
        found.extend(extra)
    return found


def resolve_plugin(
    name: str,
    *,
    extra: list[Plugin] | None = None,
    plugin_dirs: list[Path] | None = None,
) -> Plugin | None:
    """Resolve a single plugin by name (discovered local/entry-point first, then built-in)."""
    for p in discover_plugins(extra=extra, plugin_dirs=plugin_dirs):
        pname = getattr(p, "name", None) or p.__class__.__name__
        if pname == name:
            return p
    if name in _BUILTIN:
        return _load_builtin(name)
    return None


def _topo_sort_plugins(
    order: list[str],
    resolved: dict[str, Plugin],
) -> tuple[list[str], dict[str, str]]:
    """Topologically sort `order` honoring each plugin's `requires` tuple.

    Preserves user-requested relative order whenever possible. Returns
    `(sorted_names, skipped_reasons)` where cycles or missing deps are flagged.
    """
    enabled_set = set(order)
    sorted_names: list[str] = []
    skipped: dict[str, str] = {}
    state: dict[str, int] = {}  # 0=unvisited, 1=visiting, 2=done

    def visit(n: str) -> bool:
        st = state.get(n, 0)
        if st == 2:
            return n not in skipped
        if st == 1:
            skipped[n] = "circular dependency detected"
            return False
        plugin = resolved.get(n)
        if plugin is None:
            return False
        state[n] = 1
        reqs = tuple(str(r) for r in (getattr(plugin, "requires", ()) or ()))
        for req in reqs:
            if req not in enabled_set or req not in resolved:
                skipped[n] = f"missing required plugin '{req}'"
                state[n] = 2
                return False
            if not visit(req):
                skipped[n] = f"dependency '{req}' unavailable"
                state[n] = 2
                return False
        state[n] = 2
        sorted_names.append(n)
        return True

    for name in order:
        if name in resolved:
            visit(name)
    return sorted_names, skipped


def apply_plugins(
    registry: ToolRegistry,
    plugins_settings: dict[str, Any] | None = None,
    *,
    extra: list[Plugin] | None = None,
    plugin_dirs: list[Path] | None = None,
    host: Any = None,
) -> AppliedPlugins:
    """Discover, topologically sort by `requires`, mount each plugin with a
    reversible `PluginHandle`, and collect aggregate contributions."""
    cfg = plugins_settings or {}
    enabled = cfg.get("enabled")            # None -> all discovered; else allowlist+order
    per_config = cfg.get("config") or {}

    by_name: dict[str, Plugin] = {}
    for p in discover_plugins(extra=extra, plugin_dirs=plugin_dirs):
        name = getattr(p, "name", None) or p.__class__.__name__
        by_name[name] = p

    order = list(enabled) if enabled is not None else list(by_name.keys())
    resolved: dict[str, Plugin] = {}
    applied = AppliedPlugins()

    for name in order:
        plugin = by_name.get(name) or (_load_builtin(name) if name in _BUILTIN else None)
        if plugin is None:
            if enabled is not None:
                applied.skipped[name] = "not found"
                _warn(f"enabled plugin '{name}' not found")
            continue
        resolved[name] = plugin

    sorted_names, topo_skipped = _topo_sort_plugins(order, resolved)
    for sname, reason in topo_skipped.items():
        applied.skipped[sname] = reason
        _warn(f"plugin '{sname}' skipped: {reason}")

    for name in sorted_names:
        applied.mount_instance(
            name,
            resolved[name],
            registry,
            per_config.get(name, {}),
            host=host,
        )
    return applied


def list_available_plugins(
    plugins_settings: dict[str, Any] | None = None,
    *,
    applied: AppliedPlugins | None = None,
    plugin_dirs: list[Path] | None = None,
) -> list[dict[str, Any]]:
    """Summarize built-in + discovered plugins along with active state & metadata."""
    applied_names = set(applied.names if applied else [])
    applied_details = applied.details if applied else {}
    applied_skipped = applied.skipped if applied else {}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for name in _BUILTIN:
        seen.add(name)
        det = applied_details.get(name) or {}
        rows.append({
            "name": name,
            "description": BUILTIN_DESCRIPTIONS.get(name, ""),
            "builtin": True,
            "enabled": name in applied_names,
            "tools": list(det.get("tools") or []),
            "skills": list(det.get("skills") or []),
            "commands": list(det.get("commands") or []),
            "panels": list(det.get("panels") or []),
            "requires": list(det.get("requires") or []),
            "skipped_reason": applied_skipped.get(name, ""),
            "has_turn_policy": bool(det.get("has_turn_policy", False)),
            "has_middleware": bool(det.get("has_middleware", False)),
        })

    for p in discover_plugins(plugin_dirs=plugin_dirs):
        name = getattr(p, "name", None) or p.__class__.__name__
        if name in seen:
            continue
        seen.add(name)
        det = applied_details.get(name) or {}
        doc = (getattr(p, "description", None) or (p.__class__.__doc__ or "")).strip().splitlines()
        reqs = list(det.get("requires") or list(getattr(p, "requires", ()) or ()))
        rows.append({
            "name": name,
            "description": doc[0] if doc else "",
            "builtin": False,
            "enabled": name in applied_names,
            "tools": list(det.get("tools") or []),
            "skills": list(det.get("skills") or []),
            "commands": list(det.get("commands") or []),
            "panels": list(det.get("panels") or []),
            "requires": reqs,
            "skipped_reason": applied_skipped.get(name, ""),
            "has_turn_policy": bool(det.get("has_turn_policy", False)),
            "has_middleware": bool(det.get("has_middleware", False)),
        })
    return rows


def _load_builtin(name: str) -> Plugin | None:
    try:
        mod = importlib.import_module(_BUILTIN[name])
    except Exception as exc:
        _warn(f"built-in plugin '{name}' failed to import: {exc}")
        return None
    obj = getattr(mod, "PLUGIN", None)
    if obj is None:
        _warn(f"built-in plugin '{name}' has no PLUGIN")
        return None
    return _instantiate(obj)
