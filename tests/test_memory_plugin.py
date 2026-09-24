"""Memory plugin (P2): typed facts carried across sessions.

Covers the store (upsert by key, scopes, hand-written files), the tool's guards
(key / type / length / credentials), the system-prompt index, ASK-mode approval
through the loop, and the property the plugin exists for: a fact written in one
session is in the next session's system prompt.
"""
from __future__ import annotations

from typing import Any

import pytest

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.llm.base import LLMClient, LLMResponse, ToolCall
from codelet.plugins.builtin.memory import MAX_INDEX, MemoryStore, MemoryTool, parse
from codelet.plugins.loader import apply_plugins
from codelet.tools.base import ToolRegistry


@pytest.fixture
def dirs(tmp_path):
    return {"project": tmp_path / "proj", "user": tmp_path / "user"}


def _tool(dirs) -> MemoryTool:
    return MemoryTool(MemoryStore(dirs))


def _write(tool: MemoryTool, key: str, **kw: Any):
    params = {"action": "write", "key": key, "type": "feedback",
              "description": f"fact about {key}", "content": f"body of {key}", **kw}
    return tool.execute(params)


# ---------- store ----------

def test_write_then_view_roundtrip(dirs):
    tool = _tool(dirs)
    out = _write(tool, "test-naming", description="Tests are named check_*.py here",
                 content="Rename test_*.py files to check_*.py.", source="user correction")
    assert not out.is_error and "Saved" in out.output
    text = (dirs["project"] / "test-naming.md").read_text(encoding="utf-8")
    assert text.startswith("---\nkey: test-naming\ntype: feedback\n")
    assert "source: user correction" in text
    shown = tool.execute({"action": "view", "key": "test-naming"}).output
    assert "Rename test_*.py files to check_*.py." in shown


def test_write_is_an_upsert_by_key(dirs):
    tool = _tool(dirs)
    _write(tool, "db-port", description="Test DB uses port 5433")
    out = _write(tool, "db-port", description="Test DB uses port 5317")
    assert "Updated" in out.output
    files = list(dirs["project"].glob("*.md"))
    assert len(files) == 1                                   # replaced, not duplicated
    assert "5317" in files[0].read_text(encoding="utf-8")


def test_existing_key_keeps_its_scope_on_rewrite(dirs):
    tool = _tool(dirs)
    _write(tool, "prefers-short-answers", type="user", description="User wants terse replies")
    assert (dirs["user"] / "prefers-short-answers.md").exists()   # type=user -> user dir
    _write(tool, "prefers-short-answers", type="feedback", description="Terse, no summaries")
    assert not (dirs["project"] / "prefers-short-answers.md").exists()
    assert "no summaries" in (dirs["user"] / "prefers-short-answers.md").read_text(encoding="utf-8")


def test_delete(dirs):
    tool = _tool(dirs)
    _write(tool, "stale-fact")
    assert not tool.execute({"action": "delete", "key": "stale-fact"}).is_error
    assert not (dirs["project"] / "stale-fact.md").exists()
    assert tool.execute({"action": "delete", "key": "stale-fact"}).is_error


def test_hand_written_file_without_front_matter_is_indexed(dirs):
    dirs["project"].mkdir(parents=True)
    (dirs["project"] / "deploy-window.md").write_text(
        "Deploys only happen on Tuesdays.\nFriday deploys broke prod twice.\n", encoding="utf-8")
    m = parse(dirs["project"] / "deploy-window.md", "project")
    assert (m.key, m.type, m.description) == ("deploy-window", "project",
                                              "Deploys only happen on Tuesdays.")
    assert "- [project] deploy-window: Deploys only happen on Tuesdays." in MemoryStore(dirs).index()


# ---------- guards ----------

@pytest.mark.parametrize("params, needle", [
    ({"key": "Bad Key"}, "slug"),
    ({"type": "opinion"}, "type must be"),
    ({"description": "x" * 200}, "keep it under"),
    ({"content": ""}, "needs both"),
    ({"content": "the key is sk-abcdefghijklmnopqrstuvwx"}, "credential"),
    ({"content": "password: hunter2hunter2"}, "credential"),
])
def test_bad_writes_are_refused_and_nothing_is_written(dirs, params, needle):
    base = {"action": "write", "key": "ok-key", "type": "project",
            "description": "a fact", "content": "detail"}
    out = _tool(dirs).execute({**base, **params})
    assert out.is_error and needle in out.output
    assert not dirs["project"].exists() or not list(dirs["project"].glob("*.md"))


def test_preview_is_a_diff_for_writes_and_none_for_view(dirs):
    tool = _tool(dirs)
    _write(tool, "db-port", description="Test DB uses port 5433")
    diff = tool.preview_diff({"action": "write", "key": "db-port", "type": "project",
                              "description": "Test DB uses port 5317", "content": "x"})
    assert "-description: Test DB uses port 5433" in diff
    assert "+description: Test DB uses port 5317" in diff
    assert tool.preview_diff({"action": "view"}) is None


# ---------- plugin: index in the system prompt ----------

def test_index_lists_newest_first_and_is_capped(dirs):
    store = MemoryStore(dirs)
    dirs["project"].mkdir(parents=True)
    for i in range(MAX_INDEX + 3):
        (dirs["project"] / f"k{i:02d}.md").write_text(
            f"---\nkey: k{i:02d}\ntype: project\ndescription: fact {i}\n"
            f"updated: 2026-01-{(i % 28) + 1:02d}\n---\nbody\n", encoding="utf-8")
    lines = store.index().splitlines()
    assert len(lines) == MAX_INDEX + 1 and "3 older entries" in lines[-1]
    assert "2026-01-28" in lines[0]


def test_plugin_puts_index_and_rules_in_the_prompt(dirs):
    _write(_tool(dirs), "test-naming", description="Tests are named check_*.py here")
    reg = ToolRegistry()
    applied = apply_plugins(reg, {"enabled": ["memory"], "config": {"memory": {
        "dir": str(dirs["project"]), "user_dir": str(dirs["user"])}}})
    assert reg.get("memory") is not None
    section = applied.prompt_sections[0]
    assert "- [feedback] test-naming: Tests are named check_*.py here" in section
    assert "state the fact itself" in section
    assert "test-naming" in applied.commands["memory"]("")


def test_memory_is_not_loaded_unless_enabled():
    reg = ToolRegistry()
    apply_plugins(reg, {})
    assert reg.get("memory") is None


# ---------- through the loop ----------

class _Scripted(LLMClient):
    def __init__(self, script: list[LLMResponse]) -> None:
        self.script, self.calls = list(script), []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return self.script.pop(0) if self.script else LLMResponse(
            text_blocks=["ok"], raw_content=[{"type": "text", "text": "ok"}], stop_reason="end_turn")


def _save_call() -> LLMResponse:
    call = ToolCall(id="toolu_m1", name="memory", input={
        "action": "write", "key": "report-dir", "type": "feedback",
        "description": "Reports go in out/reports/, never the repo root",
        "content": "The user moved a report out of the root and asked for out/reports/."})
    return LLMResponse(tool_calls=[call], stop_reason="tool_use",
                       raw_content=[{"type": "tool_use", "id": call.id, "name": call.name,
                                     "input": call.input}])


def _agent(dirs, client, mode, confirm=None) -> AgentLoop:
    cfg = Config(permission_mode=mode, plugins={"enabled": ["memory"], "config": {"memory": {
        "dir": str(dirs["project"]), "user_dir": str(dirs["user"])}}})
    return AgentLoop(config=cfg, registry=ToolRegistry(), client=client, confirm_callback=confirm)


def test_ask_mode_write_needs_approval(dirs):
    seen: list[str] = []

    def reject(tool_name: str, diff: str) -> bool:
        seen.append(diff)
        return False

    _agent(dirs, _Scripted([_save_call()]), PermissionMode.ASK, confirm=reject).run("go")
    assert seen and "+description: Reports go in out/reports/" in seen[0]
    assert not (dirs["project"] / "report-dir.md").exists()      # rejected -> not written


def test_a_fact_saved_in_one_session_is_in_the_next_sessions_prompt(dirs):
    first = _agent(dirs, _Scripted([_save_call()]), PermissionMode.AUTO)
    assert "report-dir" not in first.context.system_prompt
    first.run("put the report somewhere sensible")
    # Loaded at session start, not rebuilt mid-session: the first prompt is unchanged.
    assert "report-dir" not in first.context.system_prompt

    second = _agent(dirs, _Scripted([]), PermissionMode.AUTO)
    assert "- [feedback] report-dir: Reports go in out/reports/, never the repo root" \
        in second.context.system_prompt
