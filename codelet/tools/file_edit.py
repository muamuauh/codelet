from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult


class FileEditTool(Tool):
    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing an exact string with a new string. "
            "Provide enough context in old_string to uniquely identify the target."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit."},
                "old_string": {"type": "string", "description": "Exact text to find (must be unique in the file)."},
                "new_string": {"type": "string", "description": "Text to replace it with."},
            },
            "required": ["path", "old_string", "new_string"],
        }

    def preview_diff(self, params: dict[str, Any]) -> str | None:
        path = Path(params.get("path", "")).expanduser()
        old = params.get("old_string", "") or ""
        new = params.get("new_string", "") or ""
        if not path.is_file():
            return f"(cannot preview: {path} not found)"
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"(cannot preview: {exc})"
        if old == "" or existing.count(old) != 1:
            # Mirror execute()'s pre-flight checks; an unhelpful diff would
            # confuse the user, so just describe the failure.
            return f"(cannot preview: old_string not uniquely matched in {path})"
        proposed = existing.replace(old, new, 1)
        diff = "".join(difflib.unified_diff(
            existing.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
            n=3,
        ))
        return diff or f"(no diff -- old_string == new_string in {path})"

    def execute(self, params: dict[str, Any]) -> ToolResult:
        filepath = Path(params["path"]).expanduser()
        old_string = params.get("old_string", "")
        new_string = params.get("new_string", "")

        if not filepath.exists():
            return ToolResult(output=f"Error: file not found: {filepath}", is_error=True)
        if not old_string:
            return ToolResult(output="Error: old_string must not be empty", is_error=True)

        try:
            content = filepath.read_text(errors="replace")
        except Exception as exc:
            return ToolResult(output=f"Error reading file: {exc}", is_error=True)

        count = content.count(old_string)
        if count == 0:
            return ToolResult(output="Error: old_string not found in file", is_error=True)
        if count > 1:
            return ToolResult(
                output=f"Error: old_string found {count} times -- must be unique. Add more context.",
                is_error=True,
            )

        new_content = content.replace(old_string, new_string, 1)
        try:
            filepath.write_text(new_content, encoding="utf-8")
        except Exception as exc:
            return ToolResult(output=f"Error writing file: {exc}", is_error=True)

        return ToolResult(output=f"Replaced 1 occurrence in {filepath}")
