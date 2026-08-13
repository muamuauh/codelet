"""Unit tests for the 6 core tools (P1)."""
from __future__ import annotations

from pathlib import Path

import pytest

from codelet.tools.base import Tool, ToolRegistry, ToolResult
from codelet.tools.bash_tool import BashTool
from codelet.tools.file_edit import FileEditTool
from codelet.tools.file_read import FileReadTool
from codelet.tools.file_write import FileWriteTool
from codelet.tools.glob_tool import GlobTool
from codelet.tools.grep_tool import GrepTool


# ---------- Registry ----------

def test_default_registry_has_seven_tools():
    reg = ToolRegistry.default()
    expected = {"bash", "read_file", "write_file", "edit_file", "glob", "grep", "web_fetch"}
    assert set(reg.names()) == expected


def test_api_schemas_match_tools():
    reg = ToolRegistry.default()
    schemas = reg.api_schemas()
    assert len(schemas) == 7
    for s in schemas:
        assert {"name", "description", "input_schema"} <= s.keys()


# ---------- Bash ----------

def test_bash_blocks_dangerous_pattern():
    tool = BashTool()
    denial = tool.check_permissions({"command": "rm -rf /"})
    assert denial is not None
    assert "dangerous" in denial.lower()


def test_bash_allows_safe_command():
    tool = BashTool()
    assert tool.check_permissions({"command": "echo hello"}) is None


def test_bash_executes_echo():
    tool = BashTool()
    result = tool.execute({"command": "echo hello-mini"})
    assert not result.is_error
    assert "hello-mini" in result.output


def test_bash_empty_command():
    result = BashTool().execute({"command": "  "})
    assert result.is_error


# ---------- FileRead / Write / Edit ----------

def test_read_write_edit_round_trip(tmp_path: Path):
    f = tmp_path / "demo.txt"

    write = FileWriteTool().execute({"path": str(f), "content": "alpha\nbeta\ngamma\n"})
    assert not write.is_error
    assert f.read_text() == "alpha\nbeta\ngamma\n"

    read = FileReadTool().execute({"path": str(f)})
    assert "alpha" in read.output and "beta" in read.output

    edit = FileEditTool().execute({
        "path": str(f),
        "old_string": "beta",
        "new_string": "BETA",
    })
    assert not edit.is_error
    assert "BETA" in f.read_text()


def test_read_missing_file(tmp_path: Path):
    result = FileReadTool().execute({"path": str(tmp_path / "nope")})
    assert result.is_error


def test_edit_requires_unique_match(tmp_path: Path):
    f = tmp_path / "dup.txt"
    f.write_text("foo\nfoo\n")
    result = FileEditTool().execute({"path": str(f), "old_string": "foo", "new_string": "bar"})
    assert result.is_error
    assert "unique" in result.output.lower()


# ---------- Glob / Grep ----------

def test_glob_finds_python_file(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y = 2\n")

    result = GlobTool().execute({"pattern": "*.py", "directory": str(tmp_path)})
    assert not result.is_error
    assert "a.py" in result.output
    assert "b.py" in result.output


def test_grep_finds_pattern(tmp_path: Path):
    f = tmp_path / "log.txt"
    f.write_text("nothing\nERROR: boom\nok\n")
    result = GrepTool().execute({"pattern": r"ERROR", "path": str(tmp_path)})
    assert not result.is_error
    assert "boom" in result.output
