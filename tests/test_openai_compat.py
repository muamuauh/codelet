"""OpenAI-compatible client translation tests (P5).

We avoid hitting the network: the openai SDK's `OpenAI` class is replaced with
a stub that records the call kwargs and returns a canned response. Then we
verify the translation in both directions:

  internal Anthropic-shape -> OpenAI Chat Completions request
  OpenAI Chat Completions response -> internal LLMResponse
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from codelet.llm.openai_compat import OpenAICompatClient


# ---------- stub the openai SDK ----------

class _StubCompletions:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        return self._response


class _StubChat:
    def __init__(self, completions: _StubCompletions) -> None:
        self.completions = completions


class _StubOpenAI:
    """Minimal stand-in for openai.OpenAI."""

    instances: list["_StubOpenAI"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.chat = _StubChat(_StubCompletions(self._next_response))
        _StubOpenAI.instances.append(self)

    @classmethod
    def _set_response(cls, response: Any) -> None:
        cls._next_response = response


def _make_client(monkeypatch: pytest.MonkeyPatch, response: Any) -> OpenAICompatClient:
    _StubOpenAI._set_response(response)
    _StubOpenAI.instances.clear()
    monkeypatch.setattr("openai.OpenAI", _StubOpenAI)
    return OpenAICompatClient(api_key="k", base_url="https://x.example/v1")


def _oa_response(*, content: str | None = None, tool_calls: list[dict] | None = None,
                 finish_reason: str = "stop", prompt_tokens: int = 7, completion_tokens: int = 11) -> Any:
    """Build an object that quacks like openai.types.chat.ChatCompletion."""
    tcs = []
    for tc in tool_calls or []:
        tcs.append(SimpleNamespace(
            id=tc["id"],
            function=SimpleNamespace(name=tc["name"], arguments=tc["arguments"]),
        ))
    msg = SimpleNamespace(content=content, tool_calls=tcs or None)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[choice], usage=usage)


# ---------- inbound translation: messages ----------

def test_assistant_with_tool_use_becomes_tool_calls(monkeypatch):
    client = _make_client(monkeypatch, _oa_response(content="ok"))
    client.chat(
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "let me check"},
                {"type": "tool_use", "id": "t1", "name": "bash",
                 "input": {"command": "ls"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": "file1\nfile2", "is_error": False},
            ]},
        ],
        system="be terse",
        tools=[{"name": "bash", "description": "run shell",
                "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}],
        model="gpt-4o",
    )
    sent = _StubOpenAI.instances[0].chat.completions.last_kwargs
    msgs = sent["messages"]
    # Ordering: system + user + assistant(text+tool_calls) + tool
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert msgs[1] == {"role": "user", "content": "hi"}
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "let me check"
    assert msgs[2]["tool_calls"][0]["id"] == "t1"
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "bash"
    # arguments must be a JSON string, not dict
    assert json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"]) == {"command": "ls"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "t1", "content": "file1\nfile2"}


def test_assistant_with_only_tool_use_has_content_none(monkeypatch):
    """OpenAI SDK rejects empty-string content when tool_calls are the only payload."""
    client = _make_client(monkeypatch, _oa_response(content="ok"))
    client.chat(
        messages=[{"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "bash", "input": {}},
        ]}],
        system="",
        tools=[],
        model="gpt-4o",
    )
    sent = _StubOpenAI.instances[0].chat.completions.last_kwargs
    assistant = sent["messages"][0]
    assert assistant["content"] is None
    assert "tool_calls" in assistant


def test_tool_result_is_error_prefixed(monkeypatch):
    """Anthropic-shaped is_error has no OpenAI equivalent; we surface it inline."""
    client = _make_client(monkeypatch, _oa_response(content="ok"))
    client.chat(
        messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "boom", "is_error": True},
        ]}],
        system="",
        tools=[],
        model="gpt-4o",
    )
    sent = _StubOpenAI.instances[0].chat.completions.last_kwargs
    tool_msg = sent["messages"][0]
    assert tool_msg["role"] == "tool"
    assert tool_msg["content"].startswith("[ERROR]")


# ---------- inbound translation: tools ----------

def test_tools_translated_to_openai_function_schema(monkeypatch):
    client = _make_client(monkeypatch, _oa_response(content="ok"))
    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[
            {"name": "bash", "description": "run shell",
             "input_schema": {"type": "object", "required": ["command"],
                              "properties": {"command": {"type": "string"}}}},
        ],
        model="gpt-4o",
    )
    sent = _StubOpenAI.instances[0].chat.completions.last_kwargs
    assert sent["tools"][0] == {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "run shell",
            "parameters": {
                "type": "object",
                "required": ["command"],
                "properties": {"command": {"type": "string"}},
            },
        },
    }


def test_no_tools_omits_tools_field(monkeypatch):
    """Some providers reject `tools=[]`; we omit the field when empty."""
    client = _make_client(monkeypatch, _oa_response(content="ok"))
    client.chat(messages=[{"role": "user", "content": "hi"}],
                system="", tools=[], model="gpt-4o")
    sent = _StubOpenAI.instances[0].chat.completions.last_kwargs
    assert "tools" not in sent


# ---------- outbound translation: response ----------

def test_text_response_normalized(monkeypatch):
    client = _make_client(monkeypatch, _oa_response(content="hello", finish_reason="stop"))
    response = client.chat(messages=[{"role": "user", "content": "hi"}],
                           system="", tools=[], model="gpt-4o")
    assert response.text_blocks == ["hello"]
    assert response.tool_calls == []
    assert response.raw_content == [{"type": "text", "text": "hello"}]
    assert response.stop_reason == "end_turn"  # mapped from "stop"
    assert response.usage["input_tokens"] == 7
    assert response.usage["output_tokens"] == 11


def test_tool_call_response_normalized(monkeypatch):
    raw = _oa_response(
        content=None,
        tool_calls=[{"id": "tcA", "name": "bash", "arguments": '{"command":"ls"}'}],
        finish_reason="tool_calls",
    )
    client = _make_client(monkeypatch, raw)
    response = client.chat(messages=[{"role": "user", "content": "hi"}],
                           system="", tools=[], model="gpt-4o")
    assert response.text_blocks == []
    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.id == "tcA"
    assert tc.name == "bash"
    assert tc.input == {"command": "ls"}
    assert response.stop_reason == "tool_use"  # mapped from "tool_calls"
    assert response.raw_content[0]["type"] == "tool_use"


def test_malformed_tool_arguments_default_to_empty_dict(monkeypatch):
    raw = _oa_response(
        content=None,
        tool_calls=[{"id": "tc1", "name": "bash", "arguments": "{not json"}],
        finish_reason="tool_calls",
    )
    client = _make_client(monkeypatch, raw)
    response = client.chat(messages=[{"role": "user", "content": "hi"}],
                           system="", tools=[], model="gpt-4o")
    assert response.tool_calls[0].input == {}


def test_finish_reason_length_mapped(monkeypatch):
    client = _make_client(monkeypatch, _oa_response(content="x", finish_reason="length"))
    response = client.chat(messages=[{"role": "user", "content": "hi"}],
                           system="", tools=[], model="gpt-4o")
    assert response.stop_reason == "max_tokens"


def test_init_passes_api_key_and_base_url(monkeypatch):
    _make_client(monkeypatch, _oa_response(content="x"))
    inst = _StubOpenAI.instances[0]
    assert inst.init_kwargs == {"api_key": "k", "base_url": "https://x.example/v1"}
