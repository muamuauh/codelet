"""Image attachments flow to the model: run_async builds image content blocks,
and the OpenAI-compat client translates them to the vision `image_url` shape."""
from __future__ import annotations

from typing import Any

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.events import RecordingSink
from codelet.llm.base import LLMClient, LLMResponse
from codelet.llm.openai_compat import OpenAICompatClient
from codelet.tools.base import ToolRegistry

IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}


class ScriptedClient(LLMClient):
    def __init__(self):
        self.last_messages = None

    def chat(self, *, messages, on_text: Any = None, **kw: Any) -> LLMResponse:
        self.last_messages = messages
        return LLMResponse(text_blocks=["seen"], raw_content=[{"type": "text", "text": "seen"}], stop_reason="end_turn")


def test_run_async_attaches_image_blocks():
    client = ScriptedClient()
    agent = AgentLoop(config=Config(permission_mode=PermissionMode.AUTO),
                      registry=ToolRegistry(), client=client, sink=RecordingSink())

    import asyncio
    asyncio.run(agent.run_async("what is this?", images=[IMG]))

    user = [m for m in agent.context.messages if m["role"] == "user"][0]
    assert isinstance(user["content"], list)
    kinds = [b.get("type") for b in user["content"]]
    assert kinds == ["text", "image"]
    assert user["content"][0]["text"] == "what is this?"


def test_openai_translation_to_image_url():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}, IMG]}]
    out = OpenAICompatClient._to_openai_messages(msgs)
    assert len(out) == 1 and out[0]["role"] == "user"
    parts = out[0]["content"]
    assert isinstance(parts, list)
    assert parts[0] == {"type": "text", "text": "hi"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,QUJD"


def test_text_only_user_still_a_string():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "plain"}]}]
    out = OpenAICompatClient._to_openai_messages(msgs)
    assert out[0]["content"] == "plain"  # unchanged: no images -> plain string
