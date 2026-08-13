from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Tool, ToolResult

MAX_RESULTS = 500


class GlobTool(Tool):
    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return "Find files matching a glob pattern. Returns paths sorted by modification time."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g. '**/*.py'). Patterns without '**/' are auto-prefixed.",
                },
                "directory": {
                    "type": "string",
                    "description": "Directory to search in (default: current directory).",
                },
            },
            "required": ["pattern"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        pattern = params["pattern"]
        directory = Path(params.get("directory", ".")).expanduser().resolve()

        if not directory.is_dir():
            return ToolResult(output=f"Error: directory not found: {directory}", is_error=True)

        if not pattern.startswith("**/") and "/" not in pattern and "\\" not in pattern:
            pattern = f"**/{pattern}"

        try:
            matches = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)

        if not matches:
            return ToolResult(output="No files matched.")

        lines = [str(p) for p in matches[:MAX_RESULTS]]
        if len(matches) > MAX_RESULTS:
            lines.append(f"... and {len(matches) - MAX_RESULTS} more")
        return ToolResult(output="\n".join(lines))
