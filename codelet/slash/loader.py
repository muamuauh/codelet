"""User-defined slash commands.

Markdown files at:
    ./.codelet/commands/<name>.md     (project-local, overrides user)
    ~/.codelet/commands/<name>.md     (user-global)

Optional YAML frontmatter (description shown in /help). Body is the prompt
template that gets injected when the user types `/<name> [args]` in the REPL.

The body can reference user args via `{args}` (the entire argument string)
or `{1}`, `{2}`, ... (positional words). Unknown placeholders are left as-is
so plain `{` characters in the body don't cause KeyErrors.

Example file `commands/audit.md`:

    ---
    description: Audit the repo for TODO comments and unused imports
    ---
    Please audit this repository:
    1. Find every TODO/FIXME comment in {args} files
    2. Report unused imports per file
    3. Group findings by file and suggest fixes
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SlashCommand:
    name: str
    body: str
    description: str = ""
    source: Path | None = None


@dataclass
class SlashCommandIndex:
    commands: dict[str, SlashCommand] = field(default_factory=dict)

    def add(self, cmd: SlashCommand) -> None:
        # Project-local registration runs after user-global; later add()
        # overrides cleanly.
        self.commands[cmd.name] = cmd

    def get(self, name: str) -> SlashCommand | None:
        return self.commands.get(name)

    def names(self) -> list[str]:
        return sorted(self.commands.keys())


def parse_command_file(path: Path) -> SlashCommand | None:
    """Parse a single command markdown file. Returns None on malformed input."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    description = ""
    body = text

    if text.lstrip().startswith("---"):
        stripped = text.lstrip()
        parts = stripped.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                fm = {}
            if isinstance(fm, dict) and isinstance(fm.get("description"), str):
                description = fm["description"].strip()
            body = parts[2]

    body = body.strip()
    if not body:
        return None
    return SlashCommand(name=path.stem, body=body, description=description, source=path)


def load_commands(
    project_dir: str | Path | None = None,
    user_dir: str | Path | None = None,
) -> SlashCommandIndex:
    """Load commands from user-global then project-local locations."""
    index = SlashCommandIndex()

    if user_dir is None:
        user_dir = Path.home() / ".codelet" / "commands"
    else:
        user_dir = Path(user_dir)

    if project_dir is None:
        project_dir = Path.cwd()
    else:
        project_dir = Path(project_dir)
    project_commands = project_dir / ".codelet" / "commands"

    for directory in (user_dir, project_commands):
        if not directory.is_dir():
            continue
        for md in sorted(directory.glob("*.md")):
            cmd = parse_command_file(md)
            if cmd is not None:
                index.add(cmd)

    return index


# A safe-substitution formatter: missing keys are left as `{key}` rather than
# raising KeyError. Using format_map with a custom dict so `{args}` and `{1}`
# work, and any other braces in the body pass through.
class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def expand_command(cmd: SlashCommand, args: str) -> str:
    """Substitute `{args}` and `{1}` / `{2}` / ... in the command body."""
    args = args or ""
    positional = args.split()
    mapping: dict[str, str] = {"args": args}
    for i, word in enumerate(positional, start=1):
        mapping[str(i)] = word

    return _PLACEHOLDER_RE.sub(
        lambda m: mapping.get(m.group(1), m.group(0)),
        cmd.body,
    )
