"""OpenAI-compatible LLM client (P5).

Works with any service that exposes the OpenAI Chat Completions API at a
configurable base_url. Verified shapes:
    - OpenAI official      (base_url=https://api.openai.com/v1)
    - DeepSeek             (base_url=https://api.deepseek.com/v1)
    - OpenRouter           (base_url=https://openrouter.ai/api/v1)
    - SiliconFlow / Moonshot / Zhipu (most claim "OpenAI-compatible")
    - Local Ollama         (base_url=http://localhost:11434/v1)
    - Generic LiteLLM / vLLM proxies

The agent loop only ever sees Anthropic-shaped internal messages; this client
translates in both directions:

    Internal (Anthropic-shape) -> OpenAI Chat Completions
        - assistant content is split into text + tool_calls
        - tool_use blocks become OpenAI `tool_calls` with stringified arguments
        - tool_result blocks become separate `role: "tool"` messages
        - top-level `system` becomes a leading `system` role message

    OpenAI response -> Internal LLMResponse
        - choices[0].message.content -> text block
        - choices[0].message.tool_calls -> tool_use blocks (arguments parsed)
        - usage.prompt_tokens / completion_tokens -> input_tokens / output_tokens

Provider quirks handled:
    - assistant.content may be `None` when only tool_calls are present (OpenAI
      requires `None`, not empty string, when tool_calls is the only payload)
    - tool_calls.function.arguments is a JSON string; we parse defensively
      since some providers occasionally emit malformed JSON for tiny tools
    - finish_reason "tool_calls" / "stop" / "length" mapped to a stable string
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from .base import LLMClient, LLMResponse, ToolCall


class OpenAICompatClient(LLMClient):
    provider_name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover -- handled in factory
            raise RuntimeError(
                "openai package not installed; run `pip install openai`"
            ) from exc

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

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
        oa_messages: list[dict[str, Any]] = []
        if system:
            oa_messages.append({"role": "system", "content": system})
        oa_messages.extend(self._to_openai_messages(messages))

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": oa_messages,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = self._to_openai_tools(tools)

        eff = (reasoning_effort or "").strip().lower()
        if eff == "off":
            kwargs["reasoning_effort"] = "none"
        elif eff in ("none", "minimal", "low", "medium", "high", "xhigh"):
            kwargs["reasoning_effort"] = eff

        if on_text is not None:
            emitted_any = False

            def _tracked_text(delta: str) -> None:
                nonlocal emitted_any
                emitted_any = True
                on_text(delta)

            def _tracked_reasoning(delta: str) -> None:
                nonlocal emitted_any
                emitted_any = True
                if on_reasoning is not None:
                    on_reasoning(delta)

            r_cb = _tracked_reasoning if on_reasoning is not None else None
            try:
                return self._chat_streaming(kwargs, _tracked_text, on_reasoning=r_cb)
            except Exception:
                # If partial text/reasoning already streamed to the UI, do NOT
                # silently re-run the entire request from scratch (which would
                # freeze mid-sentence and duplicate output).
                if emitted_any:
                    raise
                # Retry streaming without `reasoning_effort` if the model rejected it.
                if "reasoning_effort" in kwargs:
                    fallback_kw = {k: v for k, v in kwargs.items() if k != "reasoning_effort"}
                    try:
                        return self._chat_streaming(fallback_kw, _tracked_text, on_reasoning=r_cb)
                    except Exception:
                        if emitted_any:
                            raise
                # Some OpenAI-compatible relays reject `stream_options` — retry
                # streaming without it before dropping to non-streaming.
                try:
                    return self._chat_streaming(
                        kwargs, _tracked_text, on_reasoning=r_cb, include_usage=False
                    )
                except Exception:
                    if emitted_any:
                        raise
                # Final fallback: non-streaming call when upstream truly forbids streaming.
                pass

        started = time.perf_counter()
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception:
            if "reasoning_effort" not in kwargs:
                raise
            fallback_kw = {k: v for k, v in kwargs.items() if k != "reasoning_effort"}
            response = self._client.chat.completions.create(**fallback_kw)
        latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
        out = self._to_internal_response(response)
        out.usage["latency_ms"] = latency_ms
        if on_reasoning is not None:
            for r in out.reasoning_blocks:
                on_reasoning(r)
        return out

    def _chat_streaming(
        self,
        kwargs: dict[str, Any],
        on_text: "Callable[[str], None]",
        *,
        on_reasoning: "Callable[[str], None] | None" = None,
        include_usage: bool = True,
    ) -> LLMResponse:
        """Stream chunks, forward text deltas, and reassemble an LLMResponse.

        Tool-call arguments arrive fragmented across chunks (keyed by index), so
        we accumulate them and parse once at the end -- mirroring the non-stream
        path's defensive JSON handling.
        """
        stream_kwargs = dict(kwargs)
        stream_kwargs["stream"] = True
        # Ask for a trailing usage chunk; providers that support it give us
        # accurate telemetry, others simply omit it.
        if include_usage:
            stream_kwargs["stream_options"] = {"include_usage": True}

        started = time.perf_counter()
        ttft_ms: float | None = None
        text_acc = ""
        reasoning_acc = ""
        # index -> {"id", "name", "args"}
        tool_acc: dict[int, dict[str, str]] = {}
        usage: dict[str, Any] = {}
        finish = ""

        for chunk in self._client.chat.completions.create(**stream_kwargs):
            usage_obj = getattr(chunk, "usage", None)
            if usage_obj is not None:
                usage = self._extract_usage(usage_obj)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            if delta is not None:
                reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if isinstance(reasoning, str) and reasoning:
                    if ttft_ms is None:
                        ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                    reasoning_acc += reasoning
                    if on_reasoning is not None:
                        on_reasoning(reasoning)
                content = getattr(delta, "content", None)
                if content:
                    if ttft_ms is None:
                        ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                    text_acc += content
                    on_text(content)
                for tc in (getattr(delta, "tool_calls", None) or []):
                    if ttft_ms is None:
                        ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                    idx = getattr(tc, "index", 0) or 0
                    slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["args"] += fn.arguments
            if getattr(choice, "finish_reason", None):
                finish = choice.finish_reason

        usage["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        if ttft_ms is not None:
            usage["ttft_ms"] = ttft_ms
        return self._assemble_streamed(text_acc, tool_acc, usage, finish, reasoning=reasoning_acc)

    @staticmethod
    def _extract_usage(usage_obj: Any) -> dict[str, Any]:
        ipt = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(usage_obj, "completion_tokens", 0) or 0)
        usage: dict[str, Any] = {
            "input_tokens": ipt,
            "output_tokens": out_tok,
        }
        details = getattr(usage_obj, "prompt_tokens_details", None)
        details_cached = 0
        details_write = 0
        if details is not None:
            details_cached = int(getattr(details, "cached_tokens", 0) or (
                details.get("cached_tokens", 0) if isinstance(details, dict) else 0
            ) or 0)
            details_write = int(
                getattr(details, "cache_creation_tokens", 0)
                or getattr(details, "cache_write_tokens", 0)
                or (
                    (details.get("cache_creation_tokens") or details.get("cache_write_tokens") or 0)
                    if isinstance(details, dict)
                    else 0
                )
                or 0
            )
        anthropic_cached = int(getattr(usage_obj, "cache_read_input_tokens", 0) or 0)
        cached = details_cached or anthropic_cached
        if cached:
            usage["cache_read_tokens"] = cached
        cache_write = int(
            getattr(usage_obj, "cache_creation_input_tokens", 0)
            or getattr(usage_obj, "cache_write_tokens", 0)
            or details_write
            or 0
        )
        if cache_write:
            usage["cache_write_tokens"] = cache_write

        # Standard OpenAI `prompt_tokens` includes `cached_tokens`, whereas
        # Claude-passthrough relays often report uncached-only `prompt_tokens`
        # alongside Anthropic cache fields (or `prompt_tokens < cached + cache_write`).
        if (cached + cache_write) > 0:
            is_exclusive = (ipt < cached + cache_write) or (
                details_cached == 0 and (anthropic_cached > 0 or cache_write > 0)
            )
            if is_exclusive:
                usage["cache_exclusive"] = True
                usage["total_input_tokens"] = ipt + cached + cache_write
            else:
                usage["total_input_tokens"] = ipt
        return usage

    @staticmethod
    def _assemble_streamed(
        text: str,
        tool_acc: dict[int, dict[str, str]],
        usage: dict[str, Any],
        finish: str,
        *,
        reasoning: str = "",
    ) -> LLMResponse:
        text_blocks: list[str] = []
        tool_calls: list[ToolCall] = []
        raw: list[dict[str, Any]] = []

        if text:
            text_blocks.append(text)
            raw.append({"type": "text", "text": text})

        for idx in sorted(tool_acc):
            slot = tool_acc[idx]
            args_raw = slot.get("args") or ""
            try:
                parsed = json.loads(args_raw) if args_raw else {}
            except json.JSONDecodeError:
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {"value": parsed}
            tool_calls.append(ToolCall(id=slot.get("id", ""), name=slot.get("name", ""), input=parsed))
            raw.append({
                "type": "tool_use",
                "id": slot.get("id", ""),
                "name": slot.get("name", ""),
                "input": parsed,
            })

        stop_reason = {
            "tool_calls": "tool_use",
            "stop": "end_turn",
            "length": "max_tokens",
        }.get(finish or "", finish or "")

        return LLMResponse(
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            raw_content=raw,
            stop_reason=stop_reason,
            usage=usage,
            reasoning_blocks=[reasoning] if reasoning else [],
        )

    # ---------- inbound translation ----------

    @staticmethod
    def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
            for t in tools
        ]

    @classmethod
    def _to_openai_messages(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            if not isinstance(content, list):
                continue

            if role == "assistant":
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input") or {}),
                            },
                        })
                merged_text = "\n".join(t for t in text_parts if t)
                # OpenAI requires content=None (not "") when only tool_calls are present.
                msg_out: dict[str, Any] = {
                    "role": "assistant",
                    "content": merged_text or None,
                }
                if tool_calls:
                    msg_out["tool_calls"] = tool_calls
                out.append(msg_out)
            else:
                # role == "user" with list content: either tool_results or text blocks.
                tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                if tool_results:
                    # Each tool_result becomes its own `tool` role message in OpenAI.
                    for tr in tool_results:
                        body = tr.get("content")
                        if isinstance(body, list):
                            body = "".join(
                                b.get("text", "") for b in body
                                if isinstance(b, dict)
                            )
                        if tr.get("is_error"):
                            body = f"[ERROR] {body}"
                        out.append({
                            "role": "tool",
                            "tool_call_id": tr.get("tool_use_id", ""),
                            "content": str(body if body is not None else ""),
                        })
                elif any(isinstance(b, dict) and b.get("type") == "image" for b in content):
                    # Multimodal user turn: text + image parts (OpenAI vision shape).
                    parts: list[dict[str, Any]] = []
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text":
                            parts.append({"type": "text", "text": b.get("text", "")})
                        elif b.get("type") == "image":
                            url = cls._image_url(b.get("source") or {})
                            if url:
                                parts.append({"type": "image_url", "image_url": {"url": url}})
                    out.append({"role": "user", "content": parts})
                else:
                    text = "".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                    out.append({"role": "user", "content": text})
        return out

    @staticmethod
    def _image_url(source: dict[str, Any]) -> str:
        """Anthropic image `source` block -> OpenAI image_url string."""
        if source.get("type") == "base64":
            media = source.get("media_type", "image/png")
            return f"data:{media};base64,{source.get('data', '')}"
        if source.get("type") == "url":
            return str(source.get("url", ""))
        return ""

    # ---------- outbound translation ----------

    @classmethod
    def _to_internal_response(cls, response: Any) -> LLMResponse:
        text_blocks: list[str] = []
        reasoning_blocks: list[str] = []
        tool_calls: list[ToolCall] = []
        raw: list[dict[str, Any]] = []

        choice = response.choices[0] if getattr(response, "choices", None) else None
        message = getattr(choice, "message", None) if choice else None

        if message is not None:
            reasoning = getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None)
            if isinstance(reasoning, str) and reasoning:
                reasoning_blocks.append(reasoning)
            content = getattr(message, "content", None)
            if content:
                text_blocks.append(content)
                raw.append({"type": "text", "text": content})

            for tc in (getattr(message, "tool_calls", None) or []):
                fn = getattr(tc, "function", None)
                if fn is None:
                    continue
                args_raw = getattr(fn, "arguments", "") or ""
                try:
                    parsed = json.loads(args_raw) if args_raw else {}
                except json.JSONDecodeError:
                    parsed = {}
                if not isinstance(parsed, dict):
                    parsed = {"value": parsed}
                tc_id = getattr(tc, "id", "") or ""
                tc_name = getattr(fn, "name", "") or ""
                tool_calls.append(ToolCall(id=tc_id, name=tc_name, input=parsed))
                raw.append({
                    "type": "tool_use",
                    "id": tc_id,
                    "name": tc_name,
                    "input": parsed,
                })

        usage_obj = getattr(response, "usage", None)
        usage: dict[str, Any] = cls._extract_usage(usage_obj) if usage_obj is not None else {}

        finish = getattr(choice, "finish_reason", "") if choice else ""
        # Map OpenAI finish reasons onto Anthropic-shaped names so callers can
        # treat both providers uniformly.
        stop_reason = {
            "tool_calls": "tool_use",
            "stop": "end_turn",
            "length": "max_tokens",
        }.get(finish or "", finish or "")

        return LLMResponse(
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            raw_content=raw,
            stop_reason=stop_reason,
            usage=usage,
            reasoning_blocks=reasoning_blocks,
        )
