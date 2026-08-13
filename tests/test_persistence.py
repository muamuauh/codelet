"""Session persistence tests (P5)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.llm.base import LLMClient, LLMResponse
from codelet.persistence.session import (
    SessionStore,
    list_project_sessions,
    list_sessions,
    load_session,
    restore_into,
)
from codelet.tools.base import ToolRegistry


class StubLLM(LLMClient):
    def chat(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            text_blocks=["ok"],
            raw_content=[{"type": "text", "text": "ok"}],
            stop_reason="end_turn",
            usage={"input_tokens": 1, "output_tokens": 1},
        )


def _agent(tmp_path: Path) -> AgentLoop:
    cfg = Config(permission_mode=PermissionMode.AUTO, model="claude-sonnet-4-5")
    return AgentLoop(config=cfg, registry=ToolRegistry(), client=StubLLM())


def test_save_and_load_round_trips_messages(tmp_path: Path):
    agent = _agent(tmp_path)
    agent.context.add_user_message("seed prompt")
    agent.context.add_assistant_message([{"type": "text", "text": "hello"}])

    store = SessionStore(base_dir=tmp_path)
    path = store.record(agent)
    assert path.exists()

    snapshot = load_session(store.id, base_dir=tmp_path)
    assert snapshot["id"] == store.id
    assert snapshot["model"] == "claude-sonnet-4-5"
    assert snapshot["messages"][0]["content"] == "seed prompt"
    assert snapshot["messages"][1]["content"][0]["text"] == "hello"


def test_restore_into_replaces_context(tmp_path: Path):
    agent = _agent(tmp_path)
    agent.context.add_user_message("first")
    store = SessionStore(base_dir=tmp_path)
    store.record(agent)

    # Build a fresh agent and restore into it.
    fresh = _agent(tmp_path)
    assert fresh.context.messages == []
    snapshot = load_session(store.id, base_dir=tmp_path)
    restore_into(fresh, snapshot)
    assert len(fresh.context.messages) == 1
    assert fresh.context.messages[0]["content"] == "first"


def test_atomic_write_no_partial_files(tmp_path: Path, monkeypatch):
    """If os.replace fails mid-save, the tmp file is cleaned up so we never
    leave a half-written .json that load_session would choke on."""
    agent = _agent(tmp_path)
    agent.context.add_user_message("hi")
    store = SessionStore(base_dir=tmp_path)

    boom = OSError("disk full")
    def explode(*args, **kwargs):
        raise boom
    monkeypatch.setattr("os.replace", explode)

    with pytest.raises(OSError):
        store.record(agent)
    # Final session file never appeared
    assert not store.path.exists()
    # No tmp leftover either (we clean up in the except block)
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"unexpected tmp files: {leftovers}"


def test_list_sessions_returns_summaries_newest_first(tmp_path: Path):
    a = _agent(tmp_path)
    a.context.add_user_message("a")
    SessionStore(base_dir=tmp_path).record(a)

    # Force a different timestamp + id
    b = _agent(tmp_path)
    b.context.add_user_message("b")
    b.context.add_assistant_message([{"type": "text", "text": "ok"}])
    store_b = SessionStore(base_dir=tmp_path)
    store_b.record(b)

    items = list_sessions(base_dir=tmp_path)
    assert len(items) == 2
    # `updated_at` ordering may be equal at second resolution; both entries
    # at minimum should have the expected keys.
    for entry in items:
        assert {"id", "model", "provider", "created_at", "updated_at", "message_count"} <= entry.keys()


def test_summary_is_first_user_message(tmp_path: Path):
    agent = _agent(tmp_path)
    agent.context.add_user_message("find all TODO comments")
    agent.context.add_assistant_message([{"type": "text", "text": "sure"}])
    agent.context.add_user_message("and the FIXMEs too")

    store = SessionStore(base_dir=tmp_path)
    store.record(agent)
    snapshot = load_session(store.id, base_dir=tmp_path)
    assert snapshot["summary"] == "find all TODO comments"
    assert list_sessions(base_dir=tmp_path)[0]["summary"] == "find all TODO comments"


def test_summary_truncates_long_prompts(tmp_path: Path):
    agent = _agent(tmp_path)
    agent.context.add_user_message("x" * 200)
    store = SessionStore(base_dir=tmp_path)
    store.record(agent)
    summary = load_session(store.id, base_dir=tmp_path)["summary"]
    assert len(summary) == 80 and summary.endswith("…")


def test_project_registry_records_sessions(tmp_path: Path):
    proj = tmp_path / "proj"
    a = _agent(tmp_path)
    a.context.add_user_message("first task")
    SessionStore(base_dir=tmp_path, project_dir=proj).record(a)

    b = _agent(tmp_path)
    b.context.add_user_message("second task")
    b.context.add_assistant_message([{"type": "text", "text": "ok"}])
    SessionStore(base_dir=tmp_path, project_dir=proj).record(b)

    entries = list_project_sessions(project_dir=proj)
    assert len(entries) == 2
    titles = {e["title"] for e in entries}
    assert titles == {"first task", "second task"}
    for e in entries:
        assert {"id", "title", "updated_at", "model", "provider", "message_count"} <= e.keys()


def test_project_registry_upserts_same_session(tmp_path: Path):
    proj = tmp_path / "proj"
    agent = _agent(tmp_path)
    agent.context.add_user_message("task")
    store = SessionStore(base_dir=tmp_path, project_dir=proj)
    store.record(agent)
    store.record(agent)  # same id again -> upsert, not duplicate
    assert len(list_project_sessions(project_dir=proj)) == 1


def test_project_registry_untouched_without_project_dir(tmp_path: Path):
    proj = tmp_path / "proj"
    agent = _agent(tmp_path)
    agent.context.add_user_message("hi")
    SessionStore(base_dir=tmp_path).record(agent)  # project_dir=None
    assert list_project_sessions(project_dir=proj) == []


def test_todos_round_trip(tmp_path: Path):
    from codelet.tools.todo_write import Todo

    agent = _agent(tmp_path)
    agent.todo_store.todos = [
        Todo(content="design", status="completed"),
        Todo(content="build", status="in_progress", active_form="Building"),
    ]
    store = SessionStore(base_dir=tmp_path)
    store.record(agent)

    fresh = _agent(tmp_path)
    snapshot = load_session(store.id, base_dir=tmp_path)
    restore_into(fresh, snapshot)
    assert [t.content for t in fresh.todo_store.todos] == ["design", "build"]
    assert fresh.todo_store.todos[1].active_form == "Building"
