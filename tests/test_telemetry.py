"""Telemetry tests (P4)."""
from __future__ import annotations

from typing import Any

import pytest

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.llm.base import LLMClient, LLMResponse
from codelet.telemetry import Telemetry
from codelet.tools.base import ToolRegistry


def test_record_chat_priced_model():
    t = Telemetry()
    row = t.record_chat("claude-sonnet-4-5", {"input_tokens": 1_000_000, "output_tokens": 500_000})
    # 1M in × $3 + 0.5M out × $15 = $3 + $7.5 = $10.5
    assert row.cost_usd == pytest.approx(10.5, rel=1e-6)
    assert t.cumulative.cost_usd == pytest.approx(10.5, rel=1e-6)


def test_record_chat_unpriced_model_keeps_tokens_drops_cost():
    t = Telemetry()
    row = t.record_chat("unknown-model", {"input_tokens": 100, "output_tokens": 50})
    assert row.cost_usd is None
    assert row.input_tokens == 100
    assert row.output_tokens == 50
    assert t.cumulative.cost_usd is None


def test_pricing_overrides_apply():
    t = Telemetry()
    t.update_pricing({"my-model": {"input": 2.0, "output": 8.0}})
    row = t.record_chat("my-model", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert row.cost_usd == pytest.approx(10.0, rel=1e-6)


def test_begin_user_turn_resets_per_turn_view():
    t = Telemetry()
    t.record_chat("claude-sonnet-4-5", {"input_tokens": 100, "output_tokens": 100})
    t.begin_user_turn()
    t.record_chat("claude-sonnet-4-5", {"input_tokens": 200, "output_tokens": 200})

    # The render panel takes a snapshot internally; we don't easily inspect
    # rich.Panel, but the per-turn slice can be read from telemetry directly.
    after_marker = t.turns[t.last_turn_start_index:]
    assert len(after_marker) == 1
    assert after_marker[0].input_tokens == 200
    # Cumulative still reflects everything.
    assert t.cumulative.input_tokens == 300


# ---------- agent_loop integration ----------

class StubLLM(LLMClient):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(
            text_blocks=["ok"],
            raw_content=[{"type": "text", "text": "ok"}],
            stop_reason="end_turn",
            usage={"input_tokens": 123, "output_tokens": 45},
        )


@pytest.mark.asyncio
async def test_agent_loop_records_usage_per_chat_call():
    cfg = Config(permission_mode=PermissionMode.AUTO, model="claude-sonnet-4-5")
    agent = AgentLoop(config=cfg, registry=ToolRegistry(), client=StubLLM())
    await agent.run_async("hi")
    assert len(agent.telemetry.turns) == 1
    assert agent.telemetry.turns[0].input_tokens == 123
    assert agent.telemetry.turns[0].output_tokens == 45
    # Sonnet 4-5 is in the default pricing table -> cost is set
    assert agent.telemetry.turns[0].cost_usd is not None


@pytest.mark.asyncio
async def test_agent_loop_marks_per_turn_boundary():
    cfg = Config(permission_mode=PermissionMode.AUTO, model="claude-sonnet-4-5")
    agent = AgentLoop(config=cfg, registry=ToolRegistry(), client=StubLLM())
    await agent.run_async("first")
    assert agent.telemetry.last_turn_start_index == 0
    await agent.run_async("second")
    # Second call begins a new turn boundary at the start of its first chat.
    assert agent.telemetry.last_turn_start_index == 1
