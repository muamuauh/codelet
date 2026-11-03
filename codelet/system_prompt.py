"""System prompt builder.

Sections (in order): base + tool list + permission mode + skill index + CLAUDE.md.
The skill index keeps cold context small: only `name: description` lines,
bodies stay behind the `skill` tool.
"""
from __future__ import annotations

import os
from pathlib import Path

from .context import load_project_instructions
from .skills.loader import SkillIndex
from .tools.base import ToolRegistry

SYSTEM_PROMPT_TEMPLATE = """\
You are codelet, a lightweight AI coding assistant operating in the terminal.{model_section}

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

{environment_section}
## Current Permission Mode: {permission_mode}
{mode_description}
{skill_section}{project_instructions}"""

MODE_DESCRIPTIONS = {
    "ask": "In ASK mode, potentially dangerous operations require user confirmation.",
    "auto": "In AUTO mode, all operations are auto-approved (use with caution).",
    "plan": "In PLAN mode, only read-only operations are allowed; writes are blocked.",
}


def environment_section(cwd: str | Path | None = None, shell: str | None = None) -> str:
    """Where commands actually run. The model otherwise assumes a POSIX shell.

    The first eval suite (docs/evaluation.md) measured what that costs: 8 of
    the 20 failed bash calls were POSIX syntax handed to cmd.exe -- `[ -f x ]`,
    `python - <<'PY'`, `VAR=1 cmd`, `rg` -- and 7 were `git status` in a
    directory that is not a repository. Both are facts the loop knows and the
    model cannot see.
    """
    from .tools.bash_tool import resolve_shell

    here = Path(cwd or os.getcwd())
    kind = shell or resolve_shell()[0]
    in_git = any((d / ".git").exists() for d in (here, *here.parents))
    shell = {
        "pwsh": ("Windows. The `bash` tool runs commands in **PowerShell 7** (pwsh), not bash. "
                 "Use PowerShell syntax: `Test-Path x` instead of `[ -f x ]`, "
                 "`$env:NAME = 'v'; cmd` instead of `NAME=v cmd`, here-strings `@'...'@` "
                 "instead of heredocs, `& 'C:\\path with spaces\\x.exe'` to run a quoted path. "
                 "`&&` and `||` work. `ls` / `cat` / `rm` are aliases "
                 "for cmdlets and do not take POSIX flags (`ls -la` fails). Prefer the "
                 "read_file / glob / grep tools for inspecting files."),
        "cmd": ("Windows. The `bash` tool runs commands through **cmd.exe**, not bash: "
                "POSIX-only syntax fails -- `[ ... ]` tests, heredocs (`<<EOF`), "
                "`VAR=value cmd` prefixes, single-quoted arguments. `&&` and `||` work. "
                "Put anything non-trivial in `python -c \"...\"` or a script file, and "
                "inspect files with the read_file / glob / grep tools rather than the shell."),
        "sh": "POSIX. The `bash` tool runs commands through /bin/sh.",
    }[kind]
    repo = ("is a git repository" if in_git
            else "is **not** a git repository -- git commands will fail here")
    return (f"## Environment\n\n- OS / shell: {shell}\n"
            f"- Working directory: `{here}` ({repo}).\n")


def build_system_prompt(
    registry: ToolRegistry,
    permission_mode: str = "ask",
    project_dir: str | None = None,
    skill_index: SkillIndex | None = None,
    model: str | None = None,
    sections: list[str] | None = None,
    disabled_skills: "set[str] | None" = None,
) -> str:
    tool_list = "\n".join(
        f"- **{t.name}**: {t.description}" for t in registry.all_tools()
    )

    # Tell the model which model is serving it, so "what model are you?" gets a
    # truthful answer instead of a guess (the id lives in config, not the chat).
    model_section = f"\nYou are currently served by the model `{model}`." if model else ""

    skill_section = ""
    if skill_index is not None:
        summary = skill_index.index_summary(exclude=disabled_skills)
        if summary:
            skill_section = (
                "\n## Skills available (fetch on demand via the `skill` tool)\n\n"
                f"{summary}\n"
            )

    instructions = load_project_instructions(project_dir)
    project_section = ""
    if instructions:
        project_section = f"\n## Project Instructions (CLAUDE.md)\n\n{instructions}"

    # Plugin-contributed sections (e.g. a sandbox notice, RAG capabilities).
    plugin_section = ""
    for text in sections or []:
        if text and text.strip():
            plugin_section += f"\n\n{text.strip()}"

    return SYSTEM_PROMPT_TEMPLATE.format(
        model_section=model_section,
        tool_list=tool_list,
        permission_mode=permission_mode.upper(),
        mode_description=MODE_DESCRIPTIONS.get(permission_mode, ""),
        environment_section=environment_section(project_dir),
        skill_section=skill_section,
        project_instructions=project_section,
    ).strip() + plugin_section
