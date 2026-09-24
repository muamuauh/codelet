"""Context compaction tests (P4)."""
from __future__ import annotations

from typing import Any

import pytest

from codelet.config import Config
from codelet.context import ConversationContext
from codelet.llm.base import LLMClient, LLMResponse


class StubSummarizer(LLMClient):
    """Records summarization requests and returns canned summaries."""

    def __init__(self, summary: str = "concise summary of past chunk") -> None:
        self.summary = summary
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(
            text_blocks=[self.summary],
            raw_content=[{"type": "text", "text": self.summary}],
            stop_reason="end_turn",
        )


def _populate(ctx: ConversationContext, n: int) -> None:
    """Add `n` user/assistant pairs."""
    for i in range(n):
        ctx.add_user_message(f"u{i} " + "x" * 200)  # bulk to push token estimate up
        ctx.add_assistant_message([{"type": "text", "text": f"a{i}"}])


@pytest.mark.asyncio
async def test_compaction_skipped_under_threshold():
    cfg = Config(context_window=10_000, compact_threshold_ratio=0.75, compact_keep_recent=4)
    ctx = ConversationContext(config=cfg)
    client = StubSummarizer()
    _populate(ctx, 2)  # tiny conversation
    did = await ctx.compact_if_needed(client)
    assert did is False
    assert client.calls == []  # summarizer never called


@pytest.mark.asyncio
async def test_compaction_replaces_middle_with_summary():
    cfg = Config(context_window=1_000, compact_threshold_ratio=0.5, compact_keep_recent=2)
    ctx = ConversationContext(config=cfg)
    client = StubSummarizer(summary="MIDDLE_REPLACED")
    # ~20 messages of 200+ chars each pushes well past 500 tokens (threshold).
    _populate(ctx, 10)
    original_count = len(ctx.messages)
    seed_first = ctx.messages[0]
    last_two = ctx.messages[-2:]

    did = await ctx.compact_if_needed(client)
    assert did is True
    # First message preserved verbatim
    assert ctx.messages[0] == seed_first
    # Last `keep_recent` messages preserved verbatim
    assert ctx.messages[-2:] == last_two
    # Middle replaced with exactly one summary message
    middle = ctx.messages[1:-2]
    assert len(middle) == 1
    summary_msg = middle[0]
    assert summary_msg["role"] == "user"
    assert isinstance(summary_msg["content"], str)
    assert "<conversation_summary>" in summary_msg["content"]
    assert "MIDDLE_REPLACED" in summary_msg["content"]
    # Compaction shrunk the message count
    assert len(ctx.messages) < original_count
    # Counter incremented
    assert ctx.compactions == 1


@pytest.mark.asyncio
async def test_compaction_uses_compact_model_not_main_model():
    cfg = Config(
        context_window=500, compact_threshold_ratio=0.5, compact_keep_recent=2,
        model="claude-sonnet-4-5", compact_model="claude-haiku-4-5",
    )
    ctx = ConversationContext(config=cfg)
    client = StubSummarizer()
    _populate(ctx, 8)

    await ctx.compact_if_needed(client)
    # Summarizer was called with the configured compact_model
    assert client.calls
    assert client.calls[0]["model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_compaction_makes_monotonic_progress():
    """Repeated compaction must never grow the context size -- it either
    shrinks it further (when still over threshold) or no-ops."""
    # ~450 estimated tokens: over the 300 threshold before, well under after.
    cfg = Config(context_window=600, compact_threshold_ratio=0.5, compact_keep_recent=2)
    ctx = ConversationContext(config=cfg)
    client = StubSummarizer()
    _populate(ctx, 8)

    sizes = [len(ctx.messages)]
    for _ in range(5):
        await ctx.compact_if_needed(client)
        sizes.append(len(ctx.messages))
    # Strictly non-increasing: compaction never adds messages.
    assert sizes == sorted(sizes, reverse=True)
    # And it actually fired at least once.
    assert sizes[0] > sizes[-1]


def _orphan_tool_results(messages: list) -> list[str]:
    seen, orphans = set(), []
    for m in messages:
        for b in m["content"] if isinstance(m["content"], list) else []:
            if b.get("type") == "tool_use":
                seen.add(b["id"])
            elif b.get("type") == "tool_result" and b["tool_use_id"] not in seen:
                orphans.append(b["tool_use_id"])
    return orphans


def _populate_tool_rounds(ctx: ConversationContext, n: int) -> None:
    ctx.add_user_message("seed " + "x" * 200)
    for i in range(n):
        ctx.add_assistant_message([{"type": "tool_use", "id": f"t{i}", "name": "grep", "input": {}}])
        ctx.add_tool_results([{"type": "tool_result", "tool_use_id": f"t{i}", "content": "y" * 200}])


@pytest.mark.asyncio
async def test_odd_keep_recent_widens_tail_to_keep_tool_pair():
    """keep_recent=3 after a tool round would open the tail on a tool_result;
    the tail must take its tool_use along instead of orphaning it."""
    cfg = Config(context_window=1_000, compact_threshold_ratio=0.5, compact_keep_recent=3)
    ctx = ConversationContext(config=cfg)
    client = StubSummarizer()
    _populate_tool_rounds(ctx, 10)

    assert await ctx.compact_if_needed(client) is True
    assert _orphan_tool_results(ctx.messages) == []
    tail = ctx.messages[2:]                      # after seed + summary
    assert tail[0]["role"] == "assistant"        # opens on the tool_use
    assert len(tail) == 4                        # widened from 3 to keep the pair


@pytest.mark.asyncio
async def test_compaction_fires_on_message_count_below_token_threshold():
    """Many tiny tool rounds: far under the token threshold, but the message
    count has reached the ceiling's ratio -- summarize before the hard
    ceiling drops the middle without a summary."""
    cfg = Config(context_window=1_000_000, compact_threshold_ratio=0.75,
                 compact_keep_recent=4, max_context_messages=40)
    ctx = ConversationContext(config=cfg)
    client = StubSummarizer(summary="COUNT_TRIGGERED")
    ctx.add_user_message("seed")
    for i in range(15):                          # 31 messages: 1 + 15 * 2
        ctx.add_assistant_message([{"type": "tool_use", "id": f"t{i}", "name": "ls", "input": {}}])
        ctx.add_tool_results([{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}])

    assert ctx.estimate_tokens() < 1_000_000 * 0.75
    assert await ctx.compact_if_needed(client) is True
    assert "COUNT_TRIGGERED" in ctx.messages[1]["content"]
    assert _orphan_tool_results(ctx.messages) == []


# ---------- layered compaction (L1 clear / L2 structured rolling summary) ----------

def _big_tool_rounds(ctx: ConversationContext, n: int, size: int, name: str = "grep") -> None:
    ctx.add_user_message("seed task")
    for i in range(n):
        ctx.add_assistant_message([{"type": "tool_use", "id": f"t{i}", "name": name, "input": {}}])
        ctx.add_tool_results([{"type": "tool_result", "tool_use_id": f"t{i}",
                               "content": f"out{i} " + "z" * size}])


def _results(ctx: ConversationContext) -> list[str]:
    return [m["content"][0]["content"] for m in ctx.messages
            if isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]


@pytest.mark.asyncio
async def test_l1_clears_old_tool_outputs_without_an_llm_call():
    # 6 outputs of ~2k tokens: over the L1 line (10k), under the L2 line (15k).
    cfg = Config(context_window=20_000, compact_clear_ratio=0.5,
                 compact_threshold_ratio=0.75, compact_keep_recent=4)
    ctx = ConversationContext(config=cfg)
    client = StubSummarizer()
    _big_tool_rounds(ctx, 6, 8_000)

    assert await ctx.compact_if_needed(client) is True
    assert client.calls == []                    # L1 alone was enough
    assert ctx.cleared_results == 4 and ctx.compactions == 0
    results = _results(ctx)
    assert all(r.startswith("[cleared to save context: grep output, ") for r in results[:4])
    assert results[4].startswith("out4 ") and results[5].startswith("out5 ")  # tail intact
    assert _orphan_tool_results(ctx.messages) == []


@pytest.mark.asyncio
async def test_l1_placeholder_keeps_the_spill_path():
    cfg = Config(context_window=4_000, compact_clear_ratio=0.25, compact_keep_recent=2)
    ctx = ConversationContext(config=cfg)
    ctx.add_user_message("seed")
    spilled = ("head\n\n[... 99 chars omitted; full output saved to C:\\tmp\\t0.txt ...]\n\n"
               + "q" * 5_000)     # ~1.25k tokens: past the 1k L1 line
    ctx.add_assistant_message([{"type": "tool_use", "id": "s0", "name": "bash", "input": {}}])
    ctx.add_tool_results([{"type": "tool_result", "tool_use_id": "s0", "content": spilled}])
    for i in range(2):
        ctx.add_assistant_message([{"type": "tool_use", "id": f"t{i}", "name": "ls", "input": {}}])
        ctx.add_tool_results([{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}])

    await ctx.compact_if_needed(StubSummarizer())
    cleared = _results(ctx)[0]
    assert cleared.startswith("[cleared to save context: bash output")
    assert "C:\\tmp\\t0.txt" in cleared


@pytest.mark.asyncio
async def test_l2_prompt_is_structured():
    cfg = Config(context_window=1_000, compact_threshold_ratio=0.5, compact_keep_recent=2)
    ctx = ConversationContext(config=cfg)
    client = StubSummarizer()
    _populate(ctx, 10)
    await ctx.compact_if_needed(client)
    call = client.calls[0]
    from codelet.context import SUMMARY_SECTIONS
    for section in SUMMARY_SECTIONS:
        assert f"## {section}" in call["system"]   # rules in system: kept out of the summary
    assert "## Goal" not in call["messages"][0]["content"]
    assert "<previous_summary>" not in call["messages"][0]["content"]  # nothing to merge yet


@pytest.mark.asyncio
async def test_l2_is_rolling_and_pinned_state_is_reattached_not_resummarized():
    cfg = Config(context_window=1_000, compact_threshold_ratio=0.5, compact_keep_recent=2)
    ctx = ConversationContext(config=cfg)
    client = StubSummarizer(summary="FIRST-SUMMARY")
    _populate(ctx, 10)
    await ctx.compact_if_needed(client, pinned="TODO: step 3")
    assert "<current_state>\nTODO: step 3\n</current_state>" in ctx.messages[1]["content"]

    client.summary = "SECOND-SUMMARY"
    for i in range(10):
        ctx.add_user_message(f"later{i} " + "x" * 200)
        ctx.add_assistant_message([{"type": "text", "text": f"b{i}"}])
    await ctx.compact_if_needed(client, pinned="TODO: step 4")

    second_prompt = client.calls[1]["messages"][0]["content"]
    assert "<previous_summary>\nFIRST-SUMMARY\n</previous_summary>" in second_prompt
    assert "TODO: step 3" not in second_prompt       # stale pinned state is not merged
    summaries = [m for m in ctx.messages if isinstance(m["content"], str)
                 and m["content"].startswith("<conversation_summary>")]
    assert len(summaries) == 1                       # replaced, not stacked
    assert "SECOND-SUMMARY" in summaries[0]["content"]
    assert "TODO: step 4" in summaries[0]["content"]


# ---------- token budget ----------

def test_estimate_is_anchored_on_reported_usage():
    ctx = ConversationContext(config=Config())
    ctx.add_user_message("seed")
    ctx.note_usage({"input_tokens": 50_000})          # provider's count for what was sent
    assert ctx.estimate_tokens() == 50_000
    ctx.add_assistant_message([{"type": "text", "text": "a" * 400}])
    assert 50_090 < ctx.estimate_tokens() < 50_120    # anchor + heuristic for the delta


@pytest.mark.asyncio
async def test_anchor_is_dropped_when_compaction_rewrites_messages():
    cfg = Config(context_window=1_000, compact_threshold_ratio=0.5, compact_keep_recent=2)
    ctx = ConversationContext(config=cfg)
    _populate(ctx, 10)
    ctx.note_usage({"input_tokens": 900})
    assert await ctx.compact_if_needed(StubSummarizer()) is True
    assert ctx.estimate_tokens() < 900                # heuristic over the smaller list


def test_cjk_is_not_undercounted():
    from codelet.context import approx_tokens
    assert approx_tokens("x" * 800) == 200
    assert approx_tokens("上下文压缩" * 40) == 200    # ~1 token per CJK char, not len // 4
