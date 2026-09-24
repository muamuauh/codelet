"""ConversationContext tests (P1)."""
from __future__ import annotations

from codelet.config import Config
from codelet.context import ConversationContext


def test_add_messages_in_order():
    ctx = ConversationContext(config=Config())
    ctx.add_user_message("hi")
    ctx.add_assistant_message([{"type": "text", "text": "hello"}])
    msgs = ctx.get_api_messages()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


def test_tool_results_appended_as_user_turn():
    ctx = ConversationContext(config=Config())
    ctx.add_tool_results([
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False},
    ])
    msgs = ctx.get_api_messages()
    assert msgs[-1]["role"] == "user"
    assert isinstance(msgs[-1]["content"], list)
    assert msgs[-1]["content"][0]["tool_use_id"] == "t1"


def test_truncation_keeps_first_and_recent():
    cfg = Config(max_context_messages=5)
    ctx = ConversationContext(config=cfg)
    for i in range(10):
        ctx.add_user_message(f"msg-{i}")
    msgs = ctx.get_api_messages()
    assert len(msgs) == 5
    # First message preserved
    assert msgs[0]["content"] == "msg-0"
    # Last message preserved
    assert msgs[-1]["content"] == "msg-9"


def test_default_depth_is_zero():
    ctx = ConversationContext(config=Config())
    assert ctx.depth == 0


def _tool_round(ctx: ConversationContext, i: int) -> None:
    ctx.add_assistant_message([{"type": "tool_use", "id": f"t{i}", "name": "read_file", "input": {}}])
    ctx.add_tool_results([{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}])


def _orphan_tool_results(messages: list) -> list[str]:
    """tool_use_ids of tool_results with no earlier tool_use -- the API 400s on these."""
    seen, orphans = set(), []
    for m in messages:
        for b in m["content"] if isinstance(m["content"], list) else []:
            if b.get("type") == "tool_use":
                seen.add(b["id"])
            elif b.get("type") == "tool_result" and b["tool_use_id"] not in seen:
                orphans.append(b["tool_use_id"])
    return orphans


def test_truncation_never_orphans_a_tool_result():
    """Regression: 50 small tool rounds hit the 100-message ceiling while the
    token estimate is still ~700, so compaction never ran and the count cut
    kept tool_results whose tool_use it had just dropped (11 of 60 rounds)."""
    ctx = ConversationContext(config=Config(max_context_messages=100))
    ctx.add_user_message("seed")
    for i in range(60):
        _tool_round(ctx, i)
        assert _orphan_tool_results(ctx.messages) == [], f"orphan after round {i}"
        assert len(ctx.messages) <= 100
    assert ctx.messages[0]["content"] == "seed"
    assert ctx.messages[-1]["content"][0]["tool_use_id"] == "t59"  # newest result kept


def test_truncation_pairing_holds_for_odd_and_even_ceilings():
    for ceiling in (5, 6, 7, 8):
        ctx = ConversationContext(config=Config(max_context_messages=ceiling))
        ctx.add_user_message("seed")
        for i in range(20):
            _tool_round(ctx, i)
            assert _orphan_tool_results(ctx.messages) == [], (ceiling, i)
            assert len(ctx.messages) <= ceiling
