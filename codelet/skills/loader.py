"""Skill loader.

Skill files are markdown with YAML frontmatter:

    ---
    name: python-lint-review
    description: Review Python code for lint, unused imports, type issues
    triggers: [lint, review, python]      # optional, free-form hint to user
    allowed_tools: [Bash, Read, Grep]     # optional, hint only (not enforced here)
    ---
    # body...

Discovery order (project overrides user):
    1. ./.codelet/skills/*.md          (project-local)
    2. ~/.codelet/skills/*.md          (user-global)

Surfacing strategy: only `name: description` lines go into the system prompt
(see `SkillIndex.index_summary`). The full body stays behind the `Skill` tool
and is fetched on demand. This keeps cold context small.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    triggers: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] | None = None
    source: Path | None = None


@dataclass
class SkillIndex:
    skills: dict[str, Skill] = field(default_factory=dict)
    max_in_index: int = 30  # hard ceiling so index lines never bloat the system prompt

    def add(self, skill: Skill) -> None:
        # Project-local registration happens after user-global, so a later
        # add() with the same name overrides cleanly.
        self.skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def names(self) -> list[str]:
        return list(self.skills.keys())

    def index_summary(self) -> str:
        """One line per skill -- this is what the system prompt sees."""
        if not self.skills:
            return ""
        lines: list[str] = []
        for skill in list(self.skills.values())[: self.max_in_index]:
            desc = skill.description.replace("\n", " ").strip()
            if len(desc) > 80:
                desc = desc[:77] + "..."
            lines.append(f"- {skill.name}: {desc}")
        if len(self.skills) > self.max_in_index:
            lines.append(f"- (... {len(self.skills) - self.max_in_index} more skills truncated)")
        return "\n".join(lines)


def parse_skill_file(path: Path) -> Skill | None:
    """Parse a single skill markdown file. Returns None on malformed input."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    if not text.lstrip().startswith("---"):
        return None

    # Split on the two YAML fences. We use a maxsplit=2 split so the body
    # may freely contain "---" (e.g. horizontal rules in markdown).
    stripped = text.lstrip()
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return None
    _, fm_text, body = parts

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None

    name = fm.get("name")
    description = fm.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        return None

    triggers = fm.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [triggers]
    allowed_tools = fm.get("allowed_tools")
    if allowed_tools is not None and isinstance(allowed_tools, str):
        allowed_tools = [allowed_tools]

    return Skill(
        name=name.strip(),
        description=description.strip(),
        body=body.strip(),
        triggers=tuple(str(t) for t in triggers),
        allowed_tools=tuple(str(t) for t in allowed_tools) if allowed_tools else None,
        source=path,
    )


def load_skills(
    project_dir: str | Path | None = None,
    user_dir: str | Path | None = None,
) -> SkillIndex:
    """Load skills from user-global then project-local locations.

    Project-local entries override user-global entries with the same name.
    """
    index = SkillIndex()

    if user_dir is None:
        user_dir = Path.home() / ".codelet" / "skills"
    else:
        user_dir = Path(user_dir)

    if project_dir is None:
        project_dir = Path.cwd()
    else:
        project_dir = Path(project_dir)
    project_skills = project_dir / ".codelet" / "skills"

    for directory in (user_dir, project_skills):
        if not directory.is_dir():
            continue
        for md in sorted(directory.glob("*.md")):
            skill = parse_skill_file(md)
            if skill is not None:
                index.add(skill)

    return index
