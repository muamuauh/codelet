"""Hook runner + agent_loop integration tests (P4).

Cross-platform note: hook commands are shelled, so we use `python -c` to keep
tests portable between PowerShell/cmd/sh. Each hook reads JSON from stdin,
inspects it, and either writes JSON to stdout or exits non-zero.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from codelet.agent_loop import AgentLoop, PromptBlocked
from codelet.config import Config, PermissionMode
from codelet.hooks.runner import HookOutcome, HookRunner, HookSpec
from codelet.llm.base import LLMClient, LLMResponse, ToolCall
from codelet.tools.base import Tool, ToolRegistry, ToolResult


PY = sys.executable  # absolute path to current interpreter, works on Windows + POSIX


# ---------- HookSpec.matches ----------

def test_matcher_wildcard_matches_anything():
    assert HookSpec("*", "x").matches("bash")
    assert HookSpec("*", "x").matches("anything")


def test_matcher_exact_name():
    assert HookSpec("bash", "x").matches("bash")
    assert not HookSpec("bash", "x").matches("write_file")


def test_matcher_comma_list():
    spec = HookSpec("bash, write_file", "x")
    assert spec.matches("bash")
    assert spec.matches("write_file")
    assert not spec.matches("read_file")


# ---------- HookRunner.fire ----------

@pytest.mark.asyncio
async def test_pretooluse_block_via_nonzero_exit():
    """Hook exits 2 -> outcome.blocked is True."""
    cmd = f'{PY} -c "import sys; sys.stderr.write(\\"nope\\"); sys.exit(2)"'
    runner = HookRunner({"PreToolUse": [{"matcher": "*", "command": cmd}]})
    outcome = await runner.fire("PreToolUse", {"event": "PreToolUse", "tool_name": "bash"})
    assert outcome.blocked is True
    assert "nope" in outcome.block_reason


@pytest.mark.asyncio
async def test_pretooluse_input_override_via_stdout_json():
    """Hook prints JSON {tool_input: {...}} -> outcome.overrides reflects that."""
    cmd = (
        f'{PY} -c "import json,sys; '
        f'data=json.load(sys.stdin); '
        f'print(json.dumps({{\\"tool_input\\": {{\\"command\\": \\"echo overridden\\"}}}}))"'
    )
    runner = HookRunner({"PreToolUse": [{"matcher": "bash", "command": cmd}]})
    outcome = await runner.fire("PreToolUse", {
        "event": "PreToolUse", "tool_name": "bash", "tool_input": {"command": "echo orig"},
    })
    assert outcome.blocked is False
    assert outcome.overrides.get("tool_input") == {"command": "echo overridden"}


@pytest.mark.asyncio
async def test_post_hook_never_blocks():
    """Even if PostToolUse exits non-zero, outcome.blocked stays False."""
    cmd = f'{PY} -c "import sys; sys.exit(3)"'
    runner = HookRunner({"PostToolUse": [{"matcher": "*", "command": cmd}]})
    outcome = await runner.fire("PostToolUse", {"event": "PostToolUse", "tool_name": "bash"})
    assert outcome.blocked is False


@pytest.mark.asyncio
async def test_userpromptsubmit_can_rewrite_prompt():
    cmd = (
        f'{PY} -c "import json,sys; data=json.load(sys.stdin); '
        f'print(json.dumps({{\\"prompt\\": data[\\"prompt\\"].upper()}}))"'
    )
    runner = HookRunner({"UserPromptSubmit": [{"matcher": "*", "command": cmd}]})
    outcome = await runner.fire("UserPromptSubmit", {"event": "UserPromptSubmit", "prompt": "hi"})
    assert outcome.blocked is False
    assert outcome.overrides.get("prompt") == "HI"


@pytest.mark.asyncio
async def test_no_hooks_configured_returns_clean_outcome():
    runner = HookRunner({})
    outcome = await runner.fire("PreToolUse", {"event": "PreToolUse", "tool_name": "bash"})
    assert isinstance(outcome, HookOutcome)
    assert outcome.blocked is False
    assert outcome.overrides == {}


# ---------- agent_loop integration ----------

class StubLLM(LLMClient):
    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        if self.script:
            return self.script.pop(0)
        return LLMResponse(text_blocks=["(end)"], raw_content=[{"type": "text", "text": "(end)"}], stop_reason="end_turn")


class CaptureTool(Tool):
    """Records every tool_input it received."""

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "capture"

    @property
    def description(self) -> str:
        return "record input"

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"x": {"type": "string"}}}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        self.invocations.append(dict(params))
        return ToolResult(output=f"got:{params.get('x', '')}")


def _tool_resp(call: ToolCall) -> LLMResponse:
    return LLMResponse(
        tool_calls=[call],
        raw_content=[{"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}],
        stop_reason="tool_use",
    )


def _text_resp(text: str) -> LLMResponse:
    return LLMResponse(text_blocks=[text], raw_content=[{"type": "text", "text": text}], stop_reason="end_turn")


@pytest.mark.asyncio
async def test_pretooluse_hook_blocks_tool_in_loop():
    """Real loop: PreToolUse hook rejects -> tool never runs, error fed back."""
    block_cmd = f'{PY} -c "import sys; sys.stderr.write(\\"forbidden\\"); sys.exit(1)"'
    cfg = Config(
        permission_mode=PermissionMode.AUTO,
        hooks={"PreToolUse": [{"matcher": "capture", "command": block_cmd}]},
    )
    capture = CaptureTool()
    reg = ToolRegistry()
    reg.register(capture)

    client = StubLLM([
        _tool_resp(ToolCall(id="t1", name="capture", input={"x": "hello"})),
        _text_resp("recovered"),
    ])
    agent = AgentLoop(config=cfg, registry=reg, client=client)
    out = await agent.run_async("go")
    assert out == "recovered"
    # Tool was blocked -> never invoked.
    assert capture.invocations == []
    # The blocked result was fed back to the LLM as is_error=True.
    last_user = [m for m in agent.context.messages if m["role"] == "user"][-1]
    block = last_user["content"][0]
    assert block["is_error"] is True
    assert "forbidden" in block["content"]


@pytest.mark.asyncio
async def test_pretooluse_hook_can_rewrite_input_in_loop():
    """Hook prints JSON to override tool_input -> tool sees overridden value."""
    rewrite_cmd = (
        f'{PY} -c "import json,sys; '
        f'_=json.load(sys.stdin); '
        f'print(json.dumps({{\\"tool_input\\": {{\\"x\\": \\"REWRITTEN\\"}}}}))"'
    )
    cfg = Config(
        permission_mode=PermissionMode.AUTO,
        hooks={"PreToolUse": [{"matcher": "capture", "command": rewrite_cmd}]},
    )
    capture = CaptureTool()
    reg = ToolRegistry()
    reg.register(capture)

    client = StubLLM([
        _tool_resp(ToolCall(id="t1", name="capture", input={"x": "ORIG"})),
        _text_resp("ok"),
    ])
    agent = AgentLoop(config=cfg, registry=reg, client=client)
    await agent.run_async("go")
    assert capture.invocations == [{"x": "REWRITTEN"}]


@pytest.mark.asyncio
async def test_userpromptsubmit_block_raises():
    block_cmd = f'{PY} -c "import sys; sys.stderr.write(\\"shush\\"); sys.exit(1)"'
    cfg = Config(
        permission_mode=PermissionMode.AUTO,
        hooks={"UserPromptSubmit": [{"matcher": "*", "command": block_cmd}]},
    )
    reg = ToolRegistry()
    client = StubLLM([_text_resp("never reached")])
    agent = AgentLoop(config=cfg, registry=reg, client=client)
    with pytest.raises(PromptBlocked):
        await agent.run_async("hi")
    assert client.calls == []  # LLM never called


@pytest.mark.asyncio
async def test_userpromptsubmit_can_rewrite_prompt():
    rewrite_cmd = (
        f'{PY} -c "import json,sys; data=json.load(sys.stdin); '
        f'print(json.dumps({{\\"prompt\\": \\"OVERRIDE: \\" + data[\\"prompt\\"]}}))"'
    )
    cfg = Config(
        permission_mode=PermissionMode.AUTO,
        hooks={"UserPromptSubmit": [{"matcher": "*", "command": rewrite_cmd}]},
    )
    reg = ToolRegistry()
    client = StubLLM([_text_resp("ok")])
    agent = AgentLoop(config=cfg, registry=reg, client=client)
    await agent.run_async("original")
    # First message in context is the user message; should be the overridden one.
    first_user = next(m for m in agent.context.messages if m["role"] == "user")
    assert first_user["content"] == "OVERRIDE: original"
