"""TodoWrite tool -- in-memory task list shared across one agent session.

The model can replace the entire list per call (mirrors Claude Code semantics).
Each todo has content + status. The tool returns a rendered table that the
model sees on its next turn (so the list keeps roundtripping through context).

Side note: the TodoStore is held by the tool instance (not by ConversationContext)
so that subagents can either share the parent's store (passed in) or get their
own (default).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from io import StringIO
from typing import Any

from rich.console import Console
from rich.table import Table

from .base import Tool, ToolResult

VALID_STATUS = {"pending", "in_progress", "completed"}


@dataclass
class Todo:
    content: str
    status: str = "pending"
    active_form: str = ""


@dataclass
class TodoStore:
    todos: list[Todo] = field(default_factory=list)

    def replace(self, items: list[Todo]) -> None:
        self.todos = list(items)

    def render(self) -> str:
        if not self.todos:
            return "(no todos)"
        # Render through Rich into a string so the model gets the same view
        # the user sees in the REPL panel.
        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=100)
        table = Table(show_header=True, header_style="bold")
        table.add_column("#", justify="right", width=3)
        table.add_column("Status", width=12)
        table.add_column("Task")
        for i, t in enumerate(self.todos, 1):
            marker = {
                "pending": "[ ]",
                "in_progress": "[~]",
                "completed": "[x]",
            }.get(t.status, "[?]")
            label = t.active_form if t.status == "in_progress" and t.active_form else t.content
            table.add_row(str(i), f"{marker} {t.status}", label)
        console.print(table)
        return buf.getvalue().rstrip()


class TodoWriteTool(Tool):
    def __init__(self, store: TodoStore | None = None) -> None:
        self.store = store or TodoStore()

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "Replace the in-memory todo list. Use this for multi-step tasks to "
            "track progress. Provide the FULL list each call (it overwrites). "
            "Each item has: content, status (pending|in_progress|completed), "
            "and an optional activeForm shown while in_progress."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "Full replacement list of todos.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": list(sorted(VALID_STATUS))},
                            "activeForm": {"type": "string"},
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["todos"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        raw = params.get("todos")
        if not isinstance(raw, list):
            return ToolResult(output="Error: 'todos' must be a list.", is_error=True)

        items: list[Todo] = []
        in_progress_count = 0
        for entry in raw:
            if not isinstance(entry, dict):
                return ToolResult(output="Error: each todo must be an object.", is_error=True)
            content = entry.get("content")
            status = entry.get("status", "pending")
            if not isinstance(content, str) or not content.strip():
                return ToolResult(output="Error: each todo needs non-empty 'content'.", is_error=True)
            if status not in VALID_STATUS:
                return ToolResult(
                    output=f"Error: invalid status '{status}'. Allowed: {sorted(VALID_STATUS)}",
                    is_error=True,
                )
            if status == "in_progress":
                in_progress_count += 1
            items.append(Todo(
                content=content.strip(),
                status=status,
                active_form=str(entry.get("activeForm", "")).strip(),
            ))

        if in_progress_count > 1:
            return ToolResult(
                output="Error: at most one todo may be in_progress at a time.",
                is_error=True,
            )

        self.store.replace(items)
        return ToolResult(output=self.store.render())
