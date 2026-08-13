from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult


class FileWriteTool(Tool):
    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Write content to a file. Creates parent directories if missing. "
            "Overwrites if the file exists."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to the file."},
                "content": {"type": "string", "description": "The content to write."},
            },
            "required": ["path", "content"],
        }

    def preview_diff(self, params: dict[str, Any]) -> str | None:
        path = Path(params.get("path", "")).expanduser()
        proposed = params.get("content", "") or ""
        try:
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError:
            existing = ""
        label_old = str(path) if path.is_file() else f"{path} (new file)"
        diff = "".join(difflib.unified_diff(
            existing.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=label_old,
            tofile=str(path),
            n=3,
        ))
        return diff or f"(no diff -- writing identical content to {path})"

    def execute(self, params: dict[str, Any]) -> ToolResult:
        filepath = Path(params["path"]).expanduser()
        content = params.get("content", "")
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content, encoding="utf-8")
            return ToolResult(output=f"Wrote {len(content)} chars to {filepath}")
        except Exception as exc:
            return ToolResult(output=f"Error writing file: {exc}", is_error=True)
