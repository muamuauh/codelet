"""Skill loader tests (P3)."""
from __future__ import annotations

from pathlib import Path

from codelet.skills.loader import (
    SkillIndex,
    load_skills,
    parse_skill_file,
)


def _write_skill(path: Path, name: str, description: str, body: str = "Body content") -> None:
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


def test_parse_basic(tmp_path: Path):
    f = tmp_path / "demo.md"
    f.write_text(
        "---\n"
        "name: demo\n"
        "description: A demo skill\n"
        "triggers: [a, b]\n"
        "allowed_tools: [Bash]\n"
        "---\n"
        "# heading\n\nbody text\n",
        encoding="utf-8",
    )
    skill = parse_skill_file(f)
    assert skill is not None
    assert skill.name == "demo"
    assert skill.description == "A demo skill"
    assert skill.triggers == ("a", "b")
    assert skill.allowed_tools == ("Bash",)
    assert "body text" in skill.body


def test_parse_returns_none_for_missing_frontmatter(tmp_path: Path):
    f = tmp_path / "no-fm.md"
    f.write_text("just markdown, no frontmatter\n", encoding="utf-8")
    assert parse_skill_file(f) is None


def test_parse_returns_none_for_invalid_yaml(tmp_path: Path):
    f = tmp_path / "bad.md"
    f.write_text("---\nname: : [\n---\nbody", encoding="utf-8")
    assert parse_skill_file(f) is None


def test_parse_returns_none_for_missing_required_fields(tmp_path: Path):
    f = tmp_path / "missing.md"
    f.write_text("---\nname: only-name\n---\nbody", encoding="utf-8")
    assert parse_skill_file(f) is None


def test_body_can_contain_horizontal_rules(tmp_path: Path):
    """Markdown bodies often contain '---'; we only split on the first 2 fences."""
    f = tmp_path / "rules.md"
    f.write_text(
        "---\nname: r\ndescription: d\n---\n"
        "Intro\n\n---\n\nMore content after a horizontal rule\n",
        encoding="utf-8",
    )
    skill = parse_skill_file(f)
    assert skill is not None
    assert "horizontal rule" in skill.body
    assert "---" in skill.body  # the rule itself survives


def test_load_project_overrides_user(tmp_path: Path):
    user_dir = tmp_path / "user"
    project_dir = tmp_path / "proj"
    (user_dir).mkdir()
    (project_dir / ".codelet" / "skills").mkdir(parents=True)

    _write_skill(user_dir / "shared.md", "shared", "user version")
    _write_skill(project_dir / ".codelet" / "skills" / "shared.md", "shared", "project version")
    _write_skill(user_dir / "user-only.md", "user-only", "from user")

    index = load_skills(project_dir=project_dir, user_dir=user_dir)
    assert index.get("shared").description == "project version"
    assert index.get("user-only").description == "from user"


def test_index_summary_truncates_long_descriptions():
    idx = SkillIndex()
    long_desc = "x" * 200
    from codelet.skills.loader import Skill
    idx.add(Skill(name="big", description=long_desc, body="b"))
    summary = idx.index_summary()
    assert "big" in summary
    # Truncated to 80 chars + ellipsis
    line = next(l for l in summary.split("\n") if l.startswith("- big"))
    assert len(line) < 100


def test_index_summary_caps_total_count():
    idx = SkillIndex(max_in_index=2)
    from codelet.skills.loader import Skill
    for i in range(5):
        idx.add(Skill(name=f"s{i}", description=f"d{i}", body="b"))
    summary = idx.index_summary()
    assert "3 more skills truncated" in summary
