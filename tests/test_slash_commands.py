"""Slash command template tests (P5)."""
from __future__ import annotations

from pathlib import Path

from codelet.slash.loader import (
    SlashCommand,
    expand_command,
    load_commands,
    parse_command_file,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_parse_with_frontmatter(tmp_path: Path):
    f = tmp_path / "audit.md"
    _write(f,
        "---\n"
        "description: Audit repo for TODOs\n"
        "---\n"
        "Scan for {args} and report.\n"
    )
    cmd = parse_command_file(f)
    assert cmd is not None
    assert cmd.name == "audit"
    assert cmd.description == "Audit repo for TODOs"
    assert "Scan for {args}" in cmd.body


def test_parse_without_frontmatter(tmp_path: Path):
    f = tmp_path / "raw.md"
    _write(f, "Just a body, no frontmatter.\n")
    cmd = parse_command_file(f)
    assert cmd is not None
    assert cmd.description == ""
    assert "Just a body" in cmd.body


def test_parse_empty_body_returns_none(tmp_path: Path):
    f = tmp_path / "empty.md"
    _write(f, "---\ndescription: nothing\n---\n\n   \n")
    assert parse_command_file(f) is None


def test_expand_args_substitution():
    cmd = SlashCommand(name="x", body="run on {args} please", description="")
    out = expand_command(cmd, "src/foo.py src/bar.py")
    assert out == "run on src/foo.py src/bar.py please"


def test_expand_positional_args():
    cmd = SlashCommand(name="x", body="first={1} second={2} third={3}", description="")
    out = expand_command(cmd, "alpha beta")
    # Positional 1 and 2 substituted, 3 left as-is so plain `{3}` in body is safe.
    assert out == "first=alpha second=beta third={3}"


def test_expand_unknown_placeholder_left_intact():
    """Plain `{x}` in a markdown body must not cause KeyError."""
    cmd = SlashCommand(name="x", body="literal {brace} here {args}", description="")
    out = expand_command(cmd, "DATA")
    assert out == "literal {brace} here DATA"


def test_load_project_overrides_user(tmp_path: Path):
    user_dir = tmp_path / "user"
    project_dir = tmp_path / "p"
    _write(user_dir / "shared.md", "user version of {args}")
    _write(project_dir / ".codelet" / "commands" / "shared.md", "project version of {args}")
    _write(user_dir / "user-only.md", "from user")

    index = load_commands(project_dir=project_dir, user_dir=user_dir)
    assert "project version" in index.get("shared").body
    assert index.get("user-only").body == "from user"


def test_load_returns_empty_index_when_no_dirs(tmp_path: Path):
    index = load_commands(project_dir=tmp_path / "missing", user_dir=tmp_path / "absent")
    assert index.names() == []
