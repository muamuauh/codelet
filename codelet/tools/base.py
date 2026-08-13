"""Tool ABC + Registry.

P2 introduces async dispatch:
  - `execute(params)` (sync) is the default seam for blocking work.
  - `aexecute(params)` (async) is what the loop calls; it defaults to
    `asyncio.to_thread(self.execute, params)`.

Real-async tools (e.g. P5 WebFetch hitting httpx) override `aexecute`
directly and may leave `execute` raising NotImplementedError.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolResult:
    output: str
    is_error: bool = False


class Tool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def input_schema(self) -> dict[str, Any]: ...

    def check_permissions(self, params: dict[str, Any]) -> str | None:
        """Layer-1 self-check. Return None if allowed, else a denial reason."""
        return None

    def preview_diff(self, params: dict[str, Any]) -> str | None:
        """Optional: return a unified-diff preview of what `execute` would change.

        Used by the ASK-mode confirmation flow in agent_loop. None means "no
        preview available" (fall back to showing the raw params).
        """
        return None

    def execute(self, params: dict[str, Any]) -> ToolResult:
        """Synchronous implementation seam for blocking tools.

        Subclasses with blocking work (subprocess, file IO) override this.
        Async-native subclasses override `aexecute` instead and leave this
        raising.
        """
        raise NotImplementedError(f"{type(self).__name__} has no sync execute()")

    async def aexecute(self, params: dict[str, Any]) -> ToolResult:
        """Async entry point used by the agent loop.

        Default offloads `execute` to a worker thread so blocking tools
        don't stall the event loop. Async-native tools override this.
        """
        return await asyncio.to_thread(self.execute, params)

    def to_api_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def api_schemas(self) -> list[dict[str, Any]]:
        return [t.to_api_schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    @classmethod
    def default(cls) -> "ToolRegistry":
        from .bash_tool import BashTool
        from .file_read import FileReadTool
        from .file_write import FileWriteTool
        from .file_edit import FileEditTool
        from .glob_tool import GlobTool
        from .grep_tool import GrepTool
        from .web_fetch import WebFetchTool

        registry = cls()
        for tool_cls in (BashTool, FileReadTool, FileWriteTool, FileEditTool,
                         GlobTool, GrepTool, WebFetchTool):
            registry.register(tool_cls())
        return registry
