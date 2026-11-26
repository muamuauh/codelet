"""Anthropic LLM client -- thin wrapper over anthropic.Anthropic.messages.create."""
from __future__ import annotations

import time
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
        on_reasoning: "Callable[[str], None] | None" = None,
        reasoning_effort: str | None = None,
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

        eff = (reasoning_effort or "").strip().lower()
        budget_map = {"low": 2048, "medium": 6144, "high": 12288, "xhigh": 24576}
        if eff in budget_map:
            budget = budget_map[eff]
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            if kwargs["max_tokens"] <= budget:
                kwargs["max_tokens"] = budget + 2048

        started = time.perf_counter()
        if on_text is None:
            try:
                response = self._client.messages.create(**kwargs)
            except Exception:
                if "thinking" not in kwargs:
                    raise
                fb = {k: v for k, v in kwargs.items() if k != "thinking"}
                fb["max_tokens"] = max_tokens
                response = self._client.messages.create(**fb)
            out = self._assemble(response)
            out.usage["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
            if on_reasoning is not None:
                for r in out.reasoning_blocks:
                    on_reasoning(r)
            return out

        # Streaming: forward text & thinking deltas as they arrive, then assemble
        # the same LLMResponse from the final message.
        def _run_stream(req_kwargs: dict[str, Any]) -> LLMResponse:
            ttft_ms: float | None = None
            with self._client.messages.stream(**req_kwargs) as stream:
                for event in stream:
                    if getattr(event, "type", "") == "content_block_delta":
                        dtype = getattr(event.delta, "type", "")
                        if dtype == "text_delta":
                            if ttft_ms is None:
                                ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                            on_text(event.delta.text)
                        elif dtype == "thinking_delta" and on_reasoning is not None:
                            if ttft_ms is None:
                                ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                            thinking = getattr(event.delta, "thinking", "")
                            if thinking:
                                on_reasoning(thinking)
                out_res = self._assemble(stream.get_final_message())
                out_res.usage["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
                if ttft_ms is not None:
                    out_res.usage["ttft_ms"] = ttft_ms
                return out_res

        try:
            return _run_stream(kwargs)
        except Exception:
            if "thinking" not in kwargs:
                raise
            fb = {k: v for k, v in kwargs.items() if k != "thinking"}
            fb["max_tokens"] = max_tokens
            return _run_stream(fb)

    @staticmethod
    def _assemble(response: Any) -> LLMResponse:
        text_blocks: list[str] = []
        reasoning_blocks: list[str] = []
        tool_calls: list[ToolCall] = []
        raw: list[dict[str, Any]] = []

        for block in response.content:
            if block.type == "text":
                text_blocks.append(block.text)
                raw.append({"type": "text", "text": block.text})
            elif block.type == "thinking":
                t = getattr(block, "thinking", "")
                if t:
                    reasoning_blocks.append(t)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
                raw.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input),
                })

        usage: dict[str, Any] = {}
        if getattr(response, "usage", None):
            u = response.usage
            usage = {
                "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
            }
            cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
            cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
            if cache_read:
                usage["cache_read_tokens"] = cache_read
            if cache_write:
                usage["cache_write_tokens"] = cache_write

        return LLMResponse(
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            raw_content=raw,
            stop_reason=getattr(response, "stop_reason", "") or "",
            usage=usage,
            reasoning_blocks=reasoning_blocks,
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
