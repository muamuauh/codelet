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
    plugin: str | None = None        # owning plugin name (if bundled or attached via frontmatter)
    category: str = ""               # optional UI grouping tag (e.g. review, testing, docs)
    scope: str = "standalone"        # plugin | project | global | standalone


@dataclass
class SkillIndex:
    skills: dict[str, Skill] = field(default_factory=dict)
    max_in_index: int = 30  # hard ceiling so index lines never bloat the system prompt

    def add(self, skill: Skill) -> None:
        # Project-local registration happens after user-global, so a later
        # add() with the same name overrides cleanly.
        self.skills[skill.name] = skill

    def remove(self, name: str) -> None:
        self.skills.pop(name, None)

    def remove_by_plugin(self, plugin_name: str) -> list[str]:
        removed = [k for k, s in self.skills.items() if s.plugin == plugin_name]
        for k in removed:
            self.skills.pop(k, None)
        return removed

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def names(self) -> list[str]:
        return list(self.skills.keys())

    def index_summary(self, exclude: "set[str] | None" = None) -> str:
        """One line per skill -- this is what the system prompt sees.
        `exclude` hides skills the user toggled off in the GUI."""
        exclude = exclude or set()
        visible = [s for s in self.skills.values() if s.name not in exclude]
        if not visible:
            return ""
        lines: list[str] = []
        for skill in visible[: self.max_in_index]:
            desc = skill.description.replace("\n", " ").strip()
            if len(desc) > 80:
                desc = desc[:77] + "..."
            lines.append(f"- {skill.name}: {desc}")
        if len(visible) > self.max_in_index:
            lines.append(f"- (... {len(visible) - self.max_in_index} more skills truncated)")
        return "\n".join(lines)


def parse_skill_file(
    path: Path,
    *,
    scope: str = "standalone",
    plugin: str | None = None,
) -> Skill | None:
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

    fm_plugin = fm.get("plugin")
    resolved_plugin = plugin or (str(fm_plugin).strip() if isinstance(fm_plugin, str) and fm_plugin.strip() else None)
    fm_category = fm.get("category")
    category = str(fm_category).strip() if isinstance(fm_category, str) else ""
    resolved_scope = "plugin" if plugin else scope

    return Skill(
        name=name.strip(),
        description=description.strip(),
        body=body.strip(),
        triggers=tuple(str(t) for t in triggers),
        allowed_tools=tuple(str(t) for t in allowed_tools) if allowed_tools else None,
        source=path,
        plugin=resolved_plugin,
        category=category,
        scope=resolved_scope,
    )


def iter_skill_files(directory: Path) -> list[Path]:
    """Return every skill markdown file inside `directory`.

    Supports both:
      - Flat single-file skills: `<directory>/<name>.md`
      - Standard directory-bundled skills: `<directory>/<name>/SKILL.md`
    """
    if not directory.is_dir():
        return []
    found: list[Path] = []
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.is_file() and entry.suffix.lower() == ".md":
            found.append(entry)
        elif entry.is_dir():
            for cand_name in ("SKILL.md", "skill.md"):
                cand = entry / cand_name
                if cand.is_file():
                    found.append(cand)
                    break
    return found


def builtin_skills_dir() -> Path:
    return Path(__file__).resolve().parent / "builtin"


def load_skills(
    project_dir: str | Path | None = None,
    user_dir: str | Path | None = None,
    *,
    include_builtin: bool = True,
) -> SkillIndex:
    """Load skills from built-in, user-global, then project-local locations.

    Later layers override earlier ones with the same skill name.
    Supports both flat `*.md` files and `<skill-dir>/SKILL.md` bundles.
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

    layers: list[tuple[str, Path]] = []
    if include_builtin:
        layers.append(("builtin", builtin_skills_dir()))
    layers.extend([("global", user_dir), ("project", project_skills)])

    for scope_name, directory in layers:
        for md in iter_skill_files(directory):
            skill = parse_skill_file(md, scope=scope_name)
            if skill is not None:
                index.add(skill)

    return index
