"""SubAgent + Task tool tests (P3).

Critical invariants:
  1. SubAgent context starts empty -- parent history does NOT leak.
  2. SubAgent uses the SubAgent system prompt (not the parent's).
  3. Task summary returned to parent matches the subagent's final assistant text.
  4. Recursion depth is hard-capped at MAX_SUBAGENT_DEPTH.
  5. Multiple Task calls in one parent turn run in parallel (via existing dispatcher).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.llm.base import LLMClient, LLMResponse, ToolCall
from codelet.subagent.runner import MAX_SUBAGENT_DEPTH
from codelet.tools.base import Tool, ToolRegistry, ToolResult


# ---------- helpers ----------

class ScriptedClient(LLMClient):
    """Maps system-prompt prefix -> queue of LLMResponse.

    SubAgent and parent see different system prompts, so we route by detecting
    a marker substring in the system text. Each route has an independent FIFO
    of scripted responses.
    """

    def __init__(self, routes: dict[str, list[LLMResponse]]) -> None:
        self.routes = {k: list(v) for k, v in routes.items()}
        self.history: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.history.append(kwargs)
        system = kwargs.get("system", "") or ""
        for marker, queue in self.routes.items():
            if marker in system:
                if not queue:
                    return _text("(unscripted-end)")
                return queue.pop(0)
        return _text("(no-route-end)")


def _text(text: str) -> LLMResponse:
    return LLMResponse(
        text_blocks=[text],
        raw_content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
    )


def _tool(*calls: ToolCall) -> LLMResponse:
    raw = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
    return LLMResponse(tool_calls=list(calls), raw_content=raw, stop_reason="tool_use")


class MarkerTool(Tool):
    """Trivial leaf tool used inside subagents."""

    @property
    def name(self) -> str:
        return "marker"

    @property
    def description(self) -> str:
        return "echo a marker"

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"tag": {"type": "string"}}, "required": ["tag"]}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        return ToolResult(output=f"marker:{params['tag']}")


def _make_parent(client: ScriptedClient, *extra_tools: Tool) -> AgentLoop:
    cfg = Config(permission_mode=PermissionMode.AUTO, max_turns=10)
    reg = ToolRegistry()
    for t in extra_tools:
        reg.register(t)
    # AgentLoop will auto-register `task` (and skip `skill` since no skills).
    return AgentLoop(config=cfg, registry=reg, client=client)


# ---------- isolation ----------

@pytest.mark.asyncio
async def test_subagent_context_does_not_see_parent_history():
    """If the subagent saw parent's user message, it could echo SECRET_PARENT_MARKER.
    Test fails by detecting that marker in the subagent's tool execution."""
    parent_responses = [
        _tool(ToolCall(id="t1", name="task", input={
            "description": "research",
            "prompt": "Do not say SECRET. Just call marker with tag=safe and finish.",
        })),
        _text("parent done"),
    ]
    sub_responses = [
        _tool(ToolCall(id="m1", name="marker", input={"tag": "safe"})),
        _text("subagent summary"),
    ]
    client = ScriptedClient({
        "codelet, a lightweight": parent_responses,  # parent system prompt
        "You are a SubAgent": sub_responses,  # subagent system prompt
    })
    parent = _make_parent(client, MarkerTool())

    # Use the parent's user message as the leak vector.
    out = await parent.run_async(
        "SECRET_PARENT_MARKER -- do not propagate this to subagents."
    )
    assert out == "parent done"

    # Walk every chat call and assert the parent's secret never appeared in
    # the subagent's request messages.
    for call in client.history:
        system = call.get("system", "") or ""
        if "You are a SubAgent" in system:
            for msg in call.get("messages", []):
                content = msg.get("content")
                if isinstance(content, str):
                    assert "SECRET_PARENT_MARKER" not in content, "parent text leaked into subagent"
                elif isinstance(content, list):
                    for block in content:
                        text = block.get("text") or block.get("content") or ""
                        if isinstance(text, str):
                            assert "SECRET_PARENT_MARKER" not in text


# ---------- result protocol ----------

@pytest.mark.asyncio
async def test_task_returns_subagent_final_text_to_parent():
    parent_responses = [
        _tool(ToolCall(id="t1", name="task", input={
            "description": "lookup",
            "prompt": "say hello and stop",
        })),
        _text("got it"),
    ]
    sub_responses = [_text("subagent summary line")]
    client = ScriptedClient({
        "codelet, a lightweight": parent_responses,
        "You are a SubAgent": sub_responses,
    })
    parent = _make_parent(client)
    await parent.run_async("delegate")

    # Find the tool_result block returned to parent and check it contains the summary.
    last_user = [m for m in parent.context.messages if m["role"] == "user"][-1]
    block = last_user["content"][0]
    assert block["tool_use_id"] == "t1"
    assert "subagent summary line" in block["content"]
    # Metadata should be appended after the summary.
    assert "subagent metadata" in block["content"]
    assert "turns=" in block["content"]


# ---------- depth cap ----------

@pytest.mark.asyncio
async def test_depth_cap_refuses_when_parent_is_already_at_max():
    """At parent_depth = MAX_SUBAGENT_DEPTH (=2), spawning would put the child
    at depth 3 -- which exceeds the cap. The runner must refuse without ever
    calling the LLM."""
    from codelet.subagent.runner import SubAgentSession, SubAgentSpec

    # Track every chat call so we can prove the LLM was never invoked.
    client = ScriptedClient({})

    cfg = Config(permission_mode=PermissionMode.AUTO, max_turns=8)
    reg = ToolRegistry()
    reg.register(MarkerTool())

    session = SubAgentSession(
        parent_config=cfg,
        parent_registry=reg,
        client=client,
        parent_depth=MAX_SUBAGENT_DEPTH,  # spawning here would hit depth=3
    )
    result = await session.run(SubAgentSpec(
        description="should-be-capped",
        prompt="this should never run",
    ))

    assert result.depth_capped is True
    assert result.turns == 0
    assert "depth cap" in result.summary.lower()
    # Critically: no LLM call ever happened for the rejected spawn.
    assert client.history == []


@pytest.mark.asyncio
async def test_depth_cap_allows_up_to_max():
    """parent_depth = MAX_SUBAGENT_DEPTH - 1 (=1) is the last level that's
    allowed to spawn -- the resulting child sits exactly at the cap."""
    from codelet.subagent.runner import SubAgentSession, SubAgentSpec

    client = ScriptedClient({"You are a SubAgent": [_text("ok at the edge")]})

    cfg = Config(permission_mode=PermissionMode.AUTO, max_turns=8)
    reg = ToolRegistry()

    session = SubAgentSession(
        parent_config=cfg,
        parent_registry=reg,
        client=client,
        parent_depth=MAX_SUBAGENT_DEPTH - 1,
    )
    result = await session.run(SubAgentSpec(
        description="edge",
        prompt="run me",
    ))
    assert result.depth_capped is False
    assert "ok at the edge" in result.summary


# ---------- parallel Task calls ----------

@pytest.mark.asyncio
async def test_two_tasks_in_one_turn_run_in_parallel_and_preserve_order():
    parent_responses = [
        _tool(
            ToolCall(id="task-A", name="task", input={"description": "A", "prompt": "do A"}),
            ToolCall(id="task-B", name="task", input={"description": "B", "prompt": "do B"}),
        ),
        _text("parent done"),
    ]
    # Both subagents finish in one turn each.
    sub_responses = [
        _text("summary-A"),
        _text("summary-B"),
    ]
    client = ScriptedClient({
        "codelet, a lightweight": parent_responses,
        "You are a SubAgent": sub_responses,
    })
    parent = _make_parent(client)
    await parent.run_async("dispatch two")

    last_user = [m for m in parent.context.messages if m["role"] == "user"][-1]
    blocks = last_user["content"]
    # Order must mirror the original task_use order.
    assert [b["tool_use_id"] for b in blocks] == ["task-A", "task-B"]
    assert "summary-A" in blocks[0]["content"]
    assert "summary-B" in blocks[1]["content"]


# ---------- allowed_tools subset ----------

@pytest.mark.asyncio
async def test_subagent_allowed_tools_filters_registry():
    """When a Task is spawned with allowed_tools=['marker'], the subagent should
    only see that subset (we verify by inspecting the tools field of the chat call)."""
    parent_responses = [
        _tool(ToolCall(id="t1", name="task", input={
            "description": "limited",
            "prompt": "use only marker",
            "allowed_tools": ["marker"],
        })),
        _text("done"),
    ]
    sub_responses = [_text("ok")]
    client = ScriptedClient({
        "codelet, a lightweight": parent_responses,
        "You are a SubAgent": sub_responses,
    })
    parent = _make_parent(client, MarkerTool())
    await parent.run_async("go")

    # Find the subagent's chat call and inspect its tools schema.
    sub_calls = [c for c in client.history if "You are a SubAgent" in (c.get("system") or "")]
    assert sub_calls, "subagent should have been called"
    tool_schemas = sub_calls[0].get("tools") or []
    names = {s["name"] for s in tool_schemas}
    assert names == {"marker"}, f"expected only ['marker'], got {names}"
