"""TodoWrite tool tests."""
from __future__ import annotations

from codelet.tools.todo_write import TodoStore, TodoWriteTool


def test_replace_list_renders_table():
    tool = TodoWriteTool()
    result = tool.execute({"todos": [
        {"content": "design", "status": "completed"},
        {"content": "build", "status": "in_progress", "activeForm": "Building"},
        {"content": "test", "status": "pending"},
    ]})
    assert not result.is_error
    assert "design" in result.output
    assert "Building" in result.output  # active form shown for in_progress
    assert "test" in result.output


def test_rejects_multiple_in_progress():
    tool = TodoWriteTool()
    result = tool.execute({"todos": [
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "in_progress"},
    ]})
    assert result.is_error
    assert "in_progress" in result.output


def test_rejects_invalid_status():
    tool = TodoWriteTool()
    result = tool.execute({"todos": [{"content": "x", "status": "blocked"}]})
    assert result.is_error


def test_empty_content_rejected():
    tool = TodoWriteTool()
    result = tool.execute({"todos": [{"content": "", "status": "pending"}]})
    assert result.is_error


def test_store_is_shared_with_external_consumers():
    store = TodoStore()
    tool = TodoWriteTool(store)
    tool.execute({"todos": [{"content": "shared", "status": "pending"}]})
    assert len(store.todos) == 1
    assert store.todos[0].content == "shared"
    assert "shared" in store.render()


def test_overwrite_drops_old_items():
    tool = TodoWriteTool()
    tool.execute({"todos": [{"content": "first", "status": "pending"}]})
    tool.execute({"todos": [{"content": "second", "status": "pending"}]})
    assert len(tool.store.todos) == 1
    assert tool.store.todos[0].content == "second"
