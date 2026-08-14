"""Built-in plugins: sandbox overrides bash, RAG indexes + searches the
workspace, and built-ins load only when explicitly enabled."""
from __future__ import annotations

from codelet.plugins.loader import apply_plugins
from codelet.tools.base import ToolRegistry
from codelet.tools.bash_tool import BashTool


def test_builtins_not_auto_loaded():
    reg = ToolRegistry()
    reg.register(BashTool())
    applied = apply_plugins(reg, {})  # no `enabled` -> built-ins stay dormant
    assert "sandbox" not in applied.names
    assert "rag" not in applied.names
    assert type(reg.get("bash")).__name__ == "BashTool"  # core bash untouched


def test_sandbox_adds_separate_tool_when_enabled():
    reg = ToolRegistry()
    reg.register(BashTool())
    applied = apply_plugins(reg, {"enabled": ["sandbox"]})
    assert "sandbox" in applied.names
    # a separate `sandbox` tool is added; the core `bash` is left untouched
    assert type(reg.get("bash")).__name__ == "BashTool"
    assert type(reg.get("sandbox")).__name__ == "SandboxTool"
    assert applied.prompt_sections  # a "sandbox" notice is added


def test_rag_indexes_and_searches(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def find_me_special():\n    return 'unique_marker_xyz'\n")
    (tmp_path / "b.md").write_text("nothing relevant here at all\n")
    monkeypatch.chdir(tmp_path)

    reg = ToolRegistry()
    applied = apply_plugins(reg, {"enabled": ["rag"]})
    tool = reg.get("search_docs")
    assert tool is not None

    out = tool.execute({"query": "unique_marker_xyz special", "k": 3})
    assert not out.is_error
    assert "a.py" in out.output and "find_me_special" in out.output

    # the /rag command rebuilds and reports stats
    stats = applied.commands["rag"]("")
    assert "indexed" in stats and "chunks" in stats


def test_rag_search_requires_query(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reg = ToolRegistry()
    apply_plugins(reg, {"enabled": ["rag"]})
    out = reg.get("search_docs").execute({"query": "   "})
    assert out.is_error
