from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult

MAX_MATCHES = 200


class GrepTool(Tool):
    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return "Search file contents using regex. Uses ripgrep (rg) if available, else Python re."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern."},
                "path": {"type": "string", "description": "File or directory to search (default: '.')."},
                "include": {"type": "string", "description": "Glob to filter files (e.g. '*.py')."},
            },
            "required": ["pattern"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        pattern = params["pattern"]
        search_path = Path(params.get("path", ".")).expanduser().resolve()
        include = params.get("include")

        if shutil.which("rg"):
            return self._rg_search(pattern, search_path, include)
        return self._python_search(pattern, search_path, include)

    def _rg_search(self, pattern: str, path: Path, include: str | None) -> ToolResult:
        cmd = ["rg", "--no-heading", "--line-number", "--max-count", str(MAX_MATCHES), pattern, str(path)]
        if include:
            cmd.extend(["--glob", include])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = result.stdout.strip()
            if not output:
                return ToolResult(output="No matches found.")
            lines = output.split("\n")
            if len(lines) > MAX_MATCHES:
                lines = lines[:MAX_MATCHES]
                lines.append(f"... (truncated at {MAX_MATCHES} matches)")
            return ToolResult(output="\n".join(lines))
        except Exception as exc:
            return ToolResult(output=f"Error running rg: {exc}", is_error=True)

    def _python_search(self, pattern: str, path: Path, include: str | None) -> ToolResult:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolResult(output=f"Invalid regex: {exc}", is_error=True)

        matches: list[str] = []
        files = [path] if path.is_file() else sorted(path.rglob(include or "*"))

        for fp in files:
            if not fp.is_file():
                continue
            try:
                for i, line in enumerate(fp.read_text(errors="replace").splitlines(), 1):
                    if regex.search(line):
                        matches.append(f"{fp}:{i}:{line.rstrip()}")
                        if len(matches) >= MAX_MATCHES:
                            matches.append(f"... (truncated at {MAX_MATCHES} matches)")
                            return ToolResult(output="\n".join(matches))
            except Exception:
                continue

        if not matches:
            return ToolResult(output="No matches found.")
        return ToolResult(output="\n".join(matches))
