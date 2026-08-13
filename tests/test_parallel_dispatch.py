"""Parallel tool dispatch tests (P2).

Critical invariants verified here:
  1. tool_result blocks are emitted in tool_use order, even when tools finish
     in arbitrary order. Anthropic API enforces this; if we ever ship a
     reorder bug it would surface as a 400 with a confusing message.
  2. A single tool raising never cancels its siblings.
  3. Tools really run concurrently (wall-clock < sum of individual sleeps).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.llm.base import LLMClient, LLMResponse, ToolCall
from codelet.tools.base import Tool, ToolRegistry, ToolResult


class ScriptedClient(LLMClient):
    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)

    def chat(self, **kwargs: Any) -> LLMResponse:
        if not self.script:
            return LLMResponse(
                text_blocks=["(end)"],
                raw_content=[{"type": "text", "text": "(end)"}],
                stop_reason="end_turn",
            )
        return self.script.pop(0)


class SlowAsyncTool(Tool):
    """Async-native tool: sleeps for `delay`, then echoes its tag.

    Since it overrides aexecute directly, we can prove parallelism by
    measuring wall time against summed delays.
    """

    @property
    def name(self) -> str:
        return "slow"

    @property
    def description(self) -> str:
        return "sleep then echo"

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"tag": {"type": "string"}, "delay": {"type": "number"}},
            "required": ["tag"],
        }

    async def aexecute(self, params: dict[str, Any]) -> ToolResult:
        delay = float(params.get("delay", 0.1))
        await asyncio.sleep(delay)
        return ToolResult(output=f"slow:{params['tag']}")


class ExplodingTool(Tool):
    @property
    def name(self) -> str:
        return "boom"

    @property
    def description(self) -> str:
        return "always raises"

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def aexecute(self, params: dict[str, Any]) -> ToolResult:
        raise RuntimeError("kaboom")


def _tool_call_block(call: ToolCall) -> dict[str, Any]:
    return {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}


def _make_agent(client: ScriptedClient, *tools: Tool) -> AgentLoop:
    cfg = Config(permission_mode=PermissionMode.AUTO)
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return AgentLoop(config=cfg, registry=reg, client=client)


def _final_user_blocks(agent: AgentLoop) -> list[dict[str, Any]]:
    last_user = [m for m in agent.context.messages if m["role"] == "user"][-1]
    assert isinstance(last_user["content"], list)
    return last_user["content"]


# ---------- order preservation ----------

@pytest.mark.asyncio
async def test_results_preserve_tool_use_order_despite_completion_order():
    """Three tools with descending delays: completion order is reversed,
    but tool_result order must mirror the original tool_use order."""
    calls = [
        ToolCall(id="t-A", name="slow", input={"tag": "A", "delay": 0.30}),
        ToolCall(id="t-B", name="slow", input={"tag": "B", "delay": 0.10}),
        ToolCall(id="t-C", name="slow", input={"tag": "C", "delay": 0.20}),
    ]
    raw = [_tool_call_block(c) for c in calls]
    client = ScriptedClient([
        LLMResponse(tool_calls=calls, raw_content=raw, stop_reason="tool_use"),
        LLMResponse(text_blocks=["done"], raw_content=[{"type": "text", "text": "done"}], stop_reason="end_turn"),
    ])
    agent = _make_agent(client, SlowAsyncTool())

    out = await agent.run_async("go")
    assert out == "done"

    blocks = _final_user_blocks(agent)
    ids = [b["tool_use_id"] for b in blocks]
    assert ids == ["t-A", "t-B", "t-C"], "tool_result order must match tool_use order"
    contents = [b["content"] for b in blocks]
    assert contents == ["slow:A", "slow:B", "slow:C"]


# ---------- real concurrency ----------

@pytest.mark.asyncio
async def test_tools_actually_run_in_parallel():
    """Three 200ms sleeps in parallel must finish well under 600ms."""
    calls = [
        ToolCall(id=f"t{i}", name="slow", input={"tag": str(i), "delay": 0.20})
        for i in range(3)
    ]
    raw = [_tool_call_block(c) for c in calls]
    client = ScriptedClient([
        LLMResponse(tool_calls=calls, raw_content=raw, stop_reason="tool_use"),
        LLMResponse(text_blocks=["done"], raw_content=[{"type": "text", "text": "done"}], stop_reason="end_turn"),
    ])
    agent = _make_agent(client, SlowAsyncTool())

    start = time.perf_counter()
    await agent.run_async("go")
    elapsed = time.perf_counter() - start
    # Sequential would be ~0.60s; parallel should be ~0.20s. Generous bound
    # for CI noise.
    assert elapsed < 0.45, f"expected parallel speedup, took {elapsed:.2f}s"


# ---------- error isolation ----------

@pytest.mark.asyncio
async def test_one_tool_failing_does_not_affect_siblings():
    calls = [
        ToolCall(id="ok-1", name="slow", input={"tag": "first", "delay": 0.05}),
        ToolCall(id="bad",  name="boom", input={}),
        ToolCall(id="ok-2", name="slow", input={"tag": "third", "delay": 0.05}),
    ]
    raw = [_tool_call_block(c) for c in calls]
    client = ScriptedClient([
        LLMResponse(tool_calls=calls, raw_content=raw, stop_reason="tool_use"),
        LLMResponse(text_blocks=["recovered"], raw_content=[{"type": "text", "text": "recovered"}], stop_reason="end_turn"),
    ])
    agent = _make_agent(client, SlowAsyncTool(), ExplodingTool())

    await agent.run_async("go")
    blocks = _final_user_blocks(agent)
    assert [b["tool_use_id"] for b in blocks] == ["ok-1", "bad", "ok-2"]
    assert blocks[0]["is_error"] is False
    assert blocks[0]["content"] == "slow:first"
    assert blocks[1]["is_error"] is True
    assert "kaboom" in blocks[1]["content"] or "raised" in blocks[1]["content"].lower()
    assert blocks[2]["is_error"] is False
    assert blocks[2]["content"] == "slow:third"


# ---------- single-call shortcut still works ----------

@pytest.mark.asyncio
async def test_single_tool_call_still_runs():
    call = ToolCall(id="solo", name="slow", input={"tag": "X", "delay": 0.01})
    client = ScriptedClient([
        LLMResponse(tool_calls=[call], raw_content=[_tool_call_block(call)], stop_reason="tool_use"),
        LLMResponse(text_blocks=["k"], raw_content=[{"type": "text", "text": "k"}], stop_reason="end_turn"),
    ])
    agent = _make_agent(client, SlowAsyncTool())
    await agent.run_async("go")
    blocks = _final_user_blocks(agent)
    assert len(blocks) == 1
    assert blocks[0]["content"] == "slow:X"


# ---------- sync tool still works via to_thread default ----------

class SyncEchoTool(Tool):
    @property
    def name(self) -> str:
        return "secho"

    @property
    def description(self) -> str:
        return "sync echo"

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        # Pure blocking impl: no await. Should be auto-wrapped via to_thread.
        return ToolResult(output=f"secho:{params['msg']}")


@pytest.mark.asyncio
async def test_sync_tool_runs_via_to_thread_default():
    calls = [
        ToolCall(id="s1", name="secho", input={"msg": "hi"}),
        ToolCall(id="s2", name="secho", input={"msg": "ho"}),
    ]
    raw = [_tool_call_block(c) for c in calls]
    client = ScriptedClient([
        LLMResponse(tool_calls=calls, raw_content=raw, stop_reason="tool_use"),
        LLMResponse(text_blocks=["k"], raw_content=[{"type": "text", "text": "k"}], stop_reason="end_turn"),
    ])
    agent = _make_agent(client, SyncEchoTool())
    await agent.run_async("go")
    blocks = _final_user_blocks(agent)
    assert [b["content"] for b in blocks] == ["secho:hi", "secho:ho"]
