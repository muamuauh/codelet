"""Discover plugins and apply them to an agent's registry.

Discovery (mirrors how skills/commands load):
  1. Python entry points in group `codelet.plugins` (pip-installable plugins).
  2. `./.codelet/plugins/*.py` (project) and `~/.codelet/plugins/*.py` (user),
     each exposing a module-level `PLUGIN` (a Plugin instance or class).

The `plugins` block from settings.json curates them:
    {"plugins": {"enabled": ["sandbox"], "config": {"sandbox": {"image": "..."}}}}
`enabled`, if present, is an allowlist+order; `config[name]` is passed to that
plugin. A plugin that fails to load or set up is skipped with a warning -- it
never crashes the agent (same contract as hooks / compaction).
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
from .base import Plugin, PluginContext, PromptMiddleware, ToolMiddleware, TurnPolicyFn

# Built-in plugins ship with codelet but load ONLY when named in
# settings.json plugins.enabled (never auto-discovered -- a sandbox silently
# overriding bash would be nasty).
_BUILTIN = {
    "sandbox": "codelet.plugins.builtin.sandbox",
    "rag": "codelet.plugins.builtin.rag",
    "evolve": "codelet.plugins.builtin.evolve",
    "memory": "codelet.plugins.builtin.memory",
    "router": "codelet.plugins.builtin.router",
    "cockpit": "codelet.plugins.builtin.cockpit",
}

BUILTIN_DESCRIPTIONS: dict[str, str] = {
    "sandbox": "Run shell commands inside an isolated one-shot Docker container.",
    "rag": "Local BM25 full-text search across workspace files (/rag, search_docs).",
    "evolve": "Self-evolution: let the agent author and hot-load new tools (/evolve).",
    "memory": "Cross-session typed markdown memory (/memory).",
    "router": "Intent router: make read-only Q&A/planning turns unable to edit files.",
    "cockpit": "Real-time context gauge, speed/cache telemetry & ZenMux PAYG balance (/cockpit).",
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
    tool_origins: dict[str, str] = field(default_factory=dict)
    skill_origins: dict[str, str] = field(default_factory=dict)
    details: dict[str, dict[str, Any]] = field(default_factory=dict)


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
        mod_name = f"_codelet_plugin_{path.stem}"
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


def apply_plugins(
    registry: ToolRegistry,
    plugins_settings: dict[str, Any] | None = None,
    *,
    extra: list[Plugin] | None = None,
    plugin_dirs: list[Path] | None = None,
    host: Any = None,
) -> AppliedPlugins:
    """Discover, filter by settings, run each plugin's setup, and collect the
    aggregate contributions. Registry-level contributions (tools) are applied as
    a side effect; the rest is returned for the loop to wire in."""
    cfg = plugins_settings or {}
    enabled = cfg.get("enabled")            # None -> all discovered; else allowlist+order
    per_config = cfg.get("config") or {}

    by_name: dict[str, Plugin] = {}
    for p in discover_plugins(extra=extra, plugin_dirs=plugin_dirs):
        name = getattr(p, "name", None) or p.__class__.__name__
        by_name[name] = p

    order = enabled if enabled is not None else list(by_name.keys())
    applied = AppliedPlugins()
    for name in order:
        plugin = by_name.get(name) or (_load_builtin(name) if name in _BUILTIN else None)
        if plugin is None:
            if enabled is not None:
                _warn(f"enabled plugin '{name}' not found")
            continue
        before_tools = set(registry.names())
        ctx = PluginContext(registry, per_config.get(name, {}), host=host)
        try:
            plugin.setup(ctx)
        except Exception as exc:
            _warn(f"plugin '{name}' setup failed: {exc}")
            continue
        added_tools = [t for t in registry.names() if t not in before_tools]
        for tname in added_tools:
            applied.tool_origins[tname] = name
        added_skills: list[str] = []
        for sk in ctx.skills:
            tagged = replace(sk, plugin=name, scope="plugin")
            applied.skills.append(tagged)
            applied.skill_origins[tagged.name] = name
            added_skills.append(tagged.name)
        applied.names.append(name)
        applied.prompt_sections.extend(ctx.prompt_sections)
        applied.prompt_middleware.extend(ctx.prompt_middleware)
        applied.tool_middleware.extend(ctx.tool_middleware)
        applied.commands.update(ctx.commands)
        applied.turn_policies.extend(ctx.turn_policies)
        applied.details[name] = {
            "tools": added_tools,
            "skills": added_skills,
            "commands": sorted(ctx.commands.keys()),
            "has_turn_policy": bool(ctx.turn_policies),
            "has_middleware": bool(ctx.prompt_middleware or ctx.tool_middleware),
        }
    return applied


def list_available_plugins(
    plugins_settings: dict[str, Any] | None = None,
    *,
    applied: AppliedPlugins | None = None,
    plugin_dirs: list[Path] | None = None,
) -> list[dict[str, Any]]:
    """Summarize built-in + discovered plugins along with their active state."""
    applied_names = set(applied.names if applied else [])
    applied_details = applied.details if applied else {}
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
        rows.append({
            "name": name,
            "description": doc[0] if doc else "",
            "builtin": False,
            "enabled": name in applied_names,
            "tools": list(det.get("tools") or []),
            "skills": list(det.get("skills") or []),
            "commands": list(det.get("commands") or []),
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
