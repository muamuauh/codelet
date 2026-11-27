---
name: plugin-creator
description: Author and hot-mount a complete codelet plugin pack (tools + skills + commands) in Creator mode
category: meta
triggers: [create plugin, plugin pack, creator mode, new plugin, 创建插件, 插件包]
allowed_tools: [create_plugin_pack, create_tool, read_file, write_file, skill]
---

# Plugin Creator Procedure (Creator Mode)

Use this skill when the user wants a reusable **feature pack** (bundling Python tools, slash commands, and/or domain skills) that can be hot-plugged on and off as a single Plugin.

## 1. Choose the Right Abstraction

- **Pure workflow / checklist (no new Python execution)** → load `skill-creator` instead and create a standalone Skill.
- **Single quick helper tool** → call `create_tool` (when `evolve` is enabled).
- **Cohesive feature pack (tools + skills + optional command / `requires`)** → call `create_plugin_pack` (if `evolve` is enabled) or author `.codelet/plugins/<name>/plugin.py` + `skills/<skill>/SKILL.md` directly.

## 2. Design the Plugin Pack

1. Choose a short kebab-case pack `name` (e.g. `sql-kit`, `release-ops`) and a clear one-line `description`.
2. Declare `requires` if the pack consumes another plugin's service (e.g. `["memory"]` or `["cockpit"]`).
3. Keep each tool small, typed (JSON-schema `parameters`), self-contained (imports inside the function body), and mark read-only tools with `read_only: true`.
4. Pair tools with a concise `SKILL.md` explaining *when* and *in what order* to use them.

## 3. Create, Hot-Mount & Verify

1. Call `create_plugin_pack(...)` — `codelet` validates syntax, writes `.codelet/plugins/<name>/`, and **hot-mounts** the pack into the live session via `host.mount_plugin()` with reversible `dispose()` cleanup.
2. Verify by invoking one of the new tools or `skill(name=...)`, and tell the user it is now toggleable in the left sidebar under **Plugins · Packs**.
