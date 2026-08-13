"""System prompt builder.

Sections (in order): base + tool list + permission mode + skill index + CLAUDE.md.
The skill index keeps cold context small: only `name: description` lines,
bodies stay behind the `skill` tool.
"""
from __future__ import annotations

from .context import load_project_instructions
from .skills.loader import SkillIndex
from .tools.base import ToolRegistry

SYSTEM_PROMPT_TEMPLATE = """\
You are codelet, a lightweight AI coding assistant operating in the terminal.

You have access to the following tools:
{tool_list}

## Operating Rules

1. Always read a file before editing it.
2. Use tools to accomplish tasks -- don't just describe what to do.
3. Prefer non-destructive read operations when running shell commands.
4. For file edits, provide enough context in old_string to uniquely match.
5. Be concise and direct.
6. For multi-step tasks, track progress with the `todo_write` tool.
7. Delegate focused subtasks (research across many files, repetitive analysis)
   to the `task` tool -- multiple Task calls in one turn run in parallel.

## Current Permission Mode: {permission_mode}
{mode_description}
{skill_section}{project_instructions}"""

MODE_DESCRIPTIONS = {
    "ask": "In ASK mode, potentially dangerous operations require user confirmation.",
    "auto": "In AUTO mode, all operations are auto-approved (use with caution).",
    "plan": "In PLAN mode, only read-only operations are allowed; writes are blocked.",
}


def build_system_prompt(
    registry: ToolRegistry,
    permission_mode: str = "ask",
    project_dir: str | None = None,
    skill_index: SkillIndex | None = None,
) -> str:
    tool_list = "\n".join(
        f"- **{t.name}**: {t.description}" for t in registry.all_tools()
    )

    skill_section = ""
    if skill_index is not None:
        summary = skill_index.index_summary()
        if summary:
            skill_section = (
                "\n## Skills available (fetch on demand via the `skill` tool)\n\n"
                f"{summary}\n"
            )

    instructions = load_project_instructions(project_dir)
    project_section = ""
    if instructions:
        project_section = f"\n## Project Instructions (CLAUDE.md)\n\n{instructions}"

    return SYSTEM_PROMPT_TEMPLATE.format(
        tool_list=tool_list,
        permission_mode=permission_mode.upper(),
        mode_description=MODE_DESCRIPTIONS.get(permission_mode, ""),
        skill_section=skill_section,
        project_instructions=project_section,
    ).strip()
