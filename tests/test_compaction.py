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
        # Make count_tokens controllable: set self._count_factor before calls.
        self._count_factor = 1

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(
            text_blocks=[self.summary],
            raw_content=[{"type": "text", "text": self.summary}],
            stop_reason="end_turn",
        )

    def count_tokens(self, text: str) -> int:
        # 1 char ~ 1 token for predictable thresholding in tests.
        return max(1, len(text)) * self._count_factor


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
    cfg = Config(context_window=1_000, compact_threshold_ratio=0.5, compact_keep_recent=2)
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
