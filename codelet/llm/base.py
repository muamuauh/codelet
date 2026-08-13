"""LLM client abstraction.

The agent loop only ever sees `LLMClient` and Anthropic-shaped internal messages.
P1 ships only the Anthropic implementation; P5 adds OpenAI-compatible.

Internal message format (Anthropic-shaped):
    user/assistant message:
        { "role": "user" | "assistant", "content": str | list[block] }
    block types:
        { "type": "text", "text": str }
        { "type": "tool_use", "id": str, "name": str, "input": dict }
        { "type": "tool_result", "tool_use_id": str, "content": str, "is_error": bool }
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    """Provider-neutral response.

    Attributes:
        text_blocks: assistant text emitted in order
        tool_calls: tool_use blocks emitted in order
        raw_content: original content list (Anthropic shape) for re-emission to context
        stop_reason: "end_turn" | "tool_use" | "max_tokens" | str
        usage: {"input_tokens": int, "output_tokens": int}
    """

    text_blocks: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_content: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)


class LLMClient(ABC):
    """Abstract LLM client. All providers expose the same shape."""

    provider_name: str = "abstract"

    @abstractmethod
    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int = 8192,
        on_text: "Callable[[str], None] | None" = None,
    ) -> LLMResponse:
        """Synchronous completion, assembled into a single LLMResponse.

        If `on_text` is given, the provider's streaming API is used and each
        text delta is passed to the callback as it arrives. The fully assembled
        LLMResponse (text + tool_calls + usage) is still returned, so callers
        that ignore `on_text` see identical behavior either way.
        """
        ...

    def count_tokens(self, text: str) -> int:
        """Rough estimate; subclasses can override with provider-native counter."""
        return max(1, len(text) // 4)
