"""UI sink -- decouples the agent loop from *how* its progress is shown.

`AgentLoop` used to `console.print(...)` directly, which hard-wired it to a Rich
terminal. Instead it now emits to an `AgentSink`: the CLI plugs in a `RichSink`
(defined in `agent_loop.py`, reproducing the old console output verbatim), and
the web server plugs in a sink that serializes each call to a WebSocket frame.

The protocol is deliberately tiny -- one method per thing the loop wants to show:
streamed text, a tool starting/finishing, a one-off notice, and a "thinking"
status while waiting for the first token. `wants_stream` lets each sink choose
token streaming (terminals + the web UI) vs a single blocking call (piped output,
tests) without the loop knowing which is which.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, ContextManager, Iterator, Protocol, runtime_checkable


@runtime_checkable
class Status(Protocol):
    """A live-updatable "thinking…" indicator (Rich's console.status, or a no-op)."""

    def update(self, text: str) -> None: ...


@runtime_checkable
class AgentSink(Protocol):
    """Everything the agent loop wants to surface to a UI."""

    @property
    def wants_stream(self) -> bool:
        """True if this sink wants token-by-token deltas; False for one blocking call."""
        ...

    def text_delta(self, text: str) -> None:
        """A chunk of streamed assistant text."""

    def text_block(self, text: str) -> None:
        """A complete assistant text block (non-streamed path)."""

    def stream_end(self) -> None:
        """The streamed assistant line is complete (terminate it)."""

    def tool_start(self, name: str, tool_input: dict[str, Any]) -> None:
        """A tool_use block is about to run."""

    def tool_result(self, name: str, output: str, is_error: bool) -> None:
        """A tool finished (or a gate/hook produced an error result)."""

    def notice(self, message: str, *, level: str = "info") -> None:
        """A one-off message: permission denial, hook block, compaction warning."""

    def thinking(self) -> ContextManager[Status]:
        """Context manager yielding a Status shown until the first token arrives."""


class _NullStatus:
    def update(self, text: str) -> None:  # noqa: D401 - no-op
        pass


class NullSink:
    """Swallows everything. Default for headless/sub-agent use and simple tests."""

    wants_stream = False

    def text_delta(self, text: str) -> None: ...
    def text_block(self, text: str) -> None: ...
    def stream_end(self) -> None: ...
    def tool_start(self, name: str, tool_input: dict[str, Any]) -> None: ...
    def tool_result(self, name: str, output: str, is_error: bool) -> None: ...
    def notice(self, message: str, *, level: str = "info") -> None: ...

    @contextmanager
    def thinking(self) -> Iterator[Status]:
        yield _NullStatus()


class RecordingSink(NullSink):
    """Records every emission as a tuple, for asserting on loop behavior in tests."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    wants_stream = True

    def text_delta(self, text: str) -> None:
        self.events.append(("text_delta", text))

    def text_block(self, text: str) -> None:
        self.events.append(("text_block", text))

    def stream_end(self) -> None:
        self.events.append(("stream_end",))

    def tool_start(self, name: str, tool_input: dict[str, Any]) -> None:
        self.events.append(("tool_start", name, tool_input))

    def tool_result(self, name: str, output: str, is_error: bool) -> None:
        self.events.append(("tool_result", name, output, is_error))

    def notice(self, message: str, *, level: str = "info") -> None:
        self.events.append(("notice", message, level))

    def kinds(self) -> list[str]:
        return [e[0] for e in self.events]
