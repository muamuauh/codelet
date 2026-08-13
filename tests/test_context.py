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
