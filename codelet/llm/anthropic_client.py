"""Anthropic LLM client -- thin wrapper over anthropic.Anthropic.messages.create."""
from __future__ import annotations

from typing import Any, Callable

import anthropic

from .base import LLMClient, LLMResponse, ToolCall


class AnthropicClient(LLMClient):
    provider_name = "anthropic"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**kwargs)

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
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        if on_text is None:
            response = self._client.messages.create(**kwargs)
            return self._assemble(response)

        # Streaming: forward text deltas as they arrive, then assemble the same
        # LLMResponse from the final message (which carries the full tool_use
        # blocks and usage).
        with self._client.messages.stream(**kwargs) as stream:
            for event in stream:
                if (
                    getattr(event, "type", "") == "content_block_delta"
                    and getattr(event.delta, "type", "") == "text_delta"
                ):
                    on_text(event.delta.text)
            return self._assemble(stream.get_final_message())

    @staticmethod
    def _assemble(response: Any) -> LLMResponse:
        text_blocks: list[str] = []
        tool_calls: list[ToolCall] = []
        raw: list[dict[str, Any]] = []

        for block in response.content:
            if block.type == "text":
                text_blocks.append(block.text)
                raw.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
                raw.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input),
                })

        usage = {}
        if getattr(response, "usage", None):
            usage = {
                "input_tokens": getattr(response.usage, "input_tokens", 0),
                "output_tokens": getattr(response.usage, "output_tokens", 0),
            }

        return LLMResponse(
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            raw_content=raw,
            stop_reason=getattr(response, "stop_reason", "") or "",
            usage=usage,
        )

    def count_tokens(self, text: str) -> int:
        # Anthropic SDK provides a token counter; fall back to heuristic if absent.
        try:
            result = self._client.messages.count_tokens(
                model="claude-sonnet-4-5",
                messages=[{"role": "user", "content": text}],
            )
            return int(getattr(result, "input_tokens", 0) or 0) or super().count_tokens(text)
        except Exception:
            return super().count_tokens(text)
