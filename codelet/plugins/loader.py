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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..tools.base import ToolRegistry
from .base import Plugin, PluginContext, PromptMiddleware, ToolMiddleware

# Built-in plugins ship with codelet but load ONLY when named in
# settings.json plugins.enabled (never auto-discovered -- a sandbox silently
# overriding bash would be nasty).
_BUILTIN = {
    "sandbox": "codelet.plugins.builtin.sandbox",
    "rag": "codelet.plugins.builtin.rag",
    "evolve": "codelet.plugins.builtin.evolve",
}


@dataclass
class AppliedPlugins:
    names: list[str] = field(default_factory=list)
    prompt_sections: list[str] = field(default_factory=list)
    prompt_middleware: list[PromptMiddleware] = field(default_factory=list)
    tool_middleware: list[ToolMiddleware] = field(default_factory=list)
    commands: dict[str, Callable[[str], str]] = field(default_factory=dict)


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

    # 2. local files
    dirs = plugin_dirs if plugin_dirs is not None else [
        Path.cwd() / ".codelet" / "plugins",
        Path.home() / ".codelet" / "plugins",
    ]
    for base in dirs:
        if base.is_dir():
            for f in sorted(base.glob("*.py")):
                if f.name.startswith("_"):
                    continue
                p = _load_from_file(f)
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
        ctx = PluginContext(registry, per_config.get(name, {}), host=host)
        try:
            plugin.setup(ctx)
        except Exception as exc:
            _warn(f"plugin '{name}' setup failed: {exc}")
            continue
        applied.names.append(name)
        applied.prompt_sections.extend(ctx.prompt_sections)
        applied.prompt_middleware.extend(ctx.prompt_middleware)
        applied.tool_middleware.extend(ctx.tool_middleware)
        applied.commands.update(ctx.commands)
    return applied


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
