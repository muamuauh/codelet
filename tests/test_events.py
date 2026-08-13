"""The agent loop emits UI through an AgentSink, and diff-approval may be async.

These lock in the decoupling that lets the web GUI observe the loop: a
RecordingSink captures tool/text events, and an awaitable confirm callback is
awaited (approve -> runs, reject -> error result) just like the sync CLI one.
"""
from __future__ import annotations

from typing import Any

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.events import RecordingSink
from codelet.llm.base import LLMClient, LLMResponse, ToolCall
from codelet.tools.base import Tool, ToolRegistry, ToolResult


class ScriptedClient(LLMClient):
    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)

    def chat(self, *, on_text: Any = None, **kwargs: Any) -> LLMResponse:
        if on_text is not None:
            for r in self.script[:1]:
                for t in r.text_blocks:
                    on_text(t)
        return self.script.pop(0) if self.script else LLMResponse(
            text_blocks=["(end)"], raw_content=[{"type": "text", "text": "(end)"}],
            stop_reason="end_turn")


def _text(text: str) -> LLMResponse:
    return LLMResponse(text_blocks=[text], raw_content=[{"type": "text", "text": text}],
                       stop_reason="end_turn", usage={"input_tokens": 4, "output_tokens": 2})


def _tools(calls: list[ToolCall]) -> LLMResponse:
    raw = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
    return LLMResponse(tool_calls=calls, raw_content=raw, stop_reason="tool_use")


class EchoTool(Tool):
    @property
    def name(self) -> str: return "echo"
    @property
    def description(self) -> str: return "echo"
    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"msg": {"type": "string"}}}
    def execute(self, params: dict[str, Any]) -> ToolResult:
        return ToolResult(output=f"echo:{params.get('msg', '')}")


class StubWrite(Tool):
    """A write-like tool so ASK-mode diff approval kicks in."""
    @property
    def name(self) -> str: return "write_file"
    @property
    def description(self) -> str: return "write"
    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"path": {"type": "string"}}}
    def preview_diff(self, params: dict[str, Any]) -> str | None:
        return "--- a\n+++ b\n-old\n+new"
    def execute(self, params: dict[str, Any]) -> ToolResult:
        return ToolResult(output=f"wrote {params.get('path', '')}")


def _agent(client, sink, *, mode=PermissionMode.AUTO, tool=None, confirm=None):
    reg = ToolRegistry()
    reg.register(tool or EchoTool())
    return AgentLoop(config=Config(permission_mode=mode), registry=reg, client=client,
                     sink=sink, confirm_callback=confirm)


def test_sink_receives_text_and_tool_events():
    client = ScriptedClient([_tools([ToolCall("t1", "echo", {"msg": "hi"})]), _text("done")])
    sink = RecordingSink()
    out = _agent(client, sink).run("go")
    assert out == "done"
    assert ("tool_start", "echo", {"msg": "hi"}) in sink.events
    assert any(e[0] == "tool_result" and "echo:hi" in e[2] and e[3] is False for e in sink.events)
    assert any(e[0] in ("text_block", "text_delta") and "done" in e[1] for e in sink.events)


def test_async_confirm_reject_blocks_the_write():
    async def confirm_no(name: str, diff: str) -> bool:
        return False
    client = ScriptedClient([_tools([ToolCall("w", "write_file", {"path": "x.py"})]), _text("ok")])
    sink = RecordingSink()
    _agent(client, sink, mode=PermissionMode.ASK, tool=StubWrite(), confirm=confirm_no).run("write")
    assert ("notice", "rejected by user", "warn") in sink.events
    # tool never ran -> no successful tool_result, and no "wrote" output anywhere
    assert not any(e[0] == "tool_result" and "wrote" in e[2] for e in sink.events)


def test_async_confirm_approve_runs_the_write():
    async def confirm_yes(name: str, diff: str) -> bool:
        return True
    client = ScriptedClient([_tools([ToolCall("w", "write_file", {"path": "x.py"})]), _text("ok")])
    sink = RecordingSink()
    _agent(client, sink, mode=PermissionMode.ASK, tool=StubWrite(), confirm=confirm_yes).run("write")
    assert any(e[0] == "tool_result" and "wrote x.py" in e[2] and e[3] is False for e in sink.events)


def test_sync_confirm_still_supported():
    client = ScriptedClient([_tools([ToolCall("w", "write_file", {"path": "y.py"})]), _text("ok")])
    sink = RecordingSink()
    _agent(client, sink, mode=PermissionMode.ASK, tool=StubWrite(),
           confirm=lambda name, diff: True).run("write")
    assert any(e[0] == "tool_result" and "wrote y.py" in e[2] for e in sink.events)
