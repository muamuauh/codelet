"""InstallSkillTool -- download and install a Skill from GitHub or direct URL.

Marked `confirm_in_ask = True` so ASK and NORMAL modes always preview the
target destination and source URL before writing files to disk.
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..skills.installer import (
    fetch_remote_skill,
    install_fetched_skill,
    parse_github_url,
    resolve_install_root,
)
from .base import Tool, ToolResult


class InstallSkillTool(Tool):
    read_only = False
    confirm_in_ask = True

    @property
    def name(self) -> str:
        return "install_skill"

    @property
    def description(self) -> str:
        return (
            "Install a Skill directly from a GitHub URL (blob file, tree folder, repo root, "
            "or raw markdown URL) into `.codelet/skills/` (project), `~/.codelet/skills/` "
            "(global), or `.codelet/plugins/<pack>/skills/` (hot-pluggable plugin pack). "
            "Automatically validates/normalizes YAML frontmatter, audits companion scripts, "
            "and hot-reloads the skill index immediately."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "GitHub skill URL (blob/tree/repo/raw) or direct `.md` URL.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["project", "global", "plugin"],
                    "description": "Where to install: 'project' (default), 'global', or 'plugin' (skill pack).",
                },
                "plugin": {
                    "type": "string",
                    "description": "Plugin pack name (required if scope='plugin'; optional to attach a standalone skill to an existing plugin).",
                },
                "category": {
                    "type": "string",
                    "description": "Optional UI category tag (e.g. review, testing, git, docs, ops).",
                },
                "name": {
                    "type": "string",
                    "description": "Optional override for the installed skill name (kebab-case).",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace an existing skill with the same name. Default false.",
                },
            },
            "required": ["url"],
        }

    def check_permissions(self, params: dict[str, Any]) -> str | None:
        url = (params.get("url") or "").strip()
        try:
            parse_github_url(url)
        except ValueError as exc:
            return str(exc)
        scope = (params.get("scope") or "project").strip().lower()
        if scope not in ("project", "global", "plugin"):
            return "scope must be one of: project, global, plugin"
        if scope == "plugin" and not (params.get("plugin") or "").strip():
            return "plugin name is required when scope='plugin'"
        return None

    def preview_diff(self, params: dict[str, Any]) -> str | None:
        url = (params.get("url") or "").strip()
        scope = (params.get("scope") or "project").strip().lower()
        plugin = (params.get("plugin") or "").strip()
        category = (params.get("category") or "").strip()
        name = (params.get("name") or "").strip()
        try:
            dest = resolve_install_root(scope=scope, plugin=plugin)
        except ValueError:
            dest = ".codelet/skills"
        lines = [
            "--- remote skill",
            f"+++ {dest}",
            f"+ Source:   {url}",
            f"+ Scope:    {scope}",
        ]
        if name:
            lines.append(f"+ Name:     {name}")
        if plugin:
            lines.append(f"+ Plugin:   {plugin}")
        if category:
            lines.append(f"+ Category: {category}")
        if params.get("overwrite"):
            lines.append("+ Mode:     overwrite existing")
        return "\n".join(lines)

    async def aexecute(self, params: dict[str, Any]) -> ToolResult:
        url = (params.get("url") or "").strip()
        scope = (params.get("scope") or "project").strip().lower()
        plugin = (params.get("plugin") or "").strip()
        category = (params.get("category") or "").strip()
        name = (params.get("name") or "").strip()
        overwrite = bool(params.get("overwrite", False))

        try:
            fetched = await asyncio.to_thread(fetch_remote_skill, url)
            res = await asyncio.to_thread(
                install_fetched_skill,
                fetched,
                scope=scope,
                plugin=plugin,
                category=category,
                name=name,
                overwrite=overwrite,
            )
        except Exception as exc:
            return ToolResult(output=f"Failed to install skill: {exc}", is_error=True)

        lines = [
            f"Installed skill `{res.name}` ({res.scope})",
            f"Location: {res.target_path}",
            f"Files ({len(res.files_written)}): " + ", ".join(p.name for p in res.files_written[:10]),
        ]
        if res.plugin:
            lines.append(f"Plugin: {res.plugin}")
        if res.category:
            lines.append(f"Category: {res.category}")
        if res.warnings:
            lines.append("")
            lines.append("Audit notices:")
            for w in res.warnings:
                lines.append(f"  - {w}")
        return ToolResult(output="\n".join(lines))
