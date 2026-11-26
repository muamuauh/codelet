"""Tests for dynamic model catalog fetching, ZenMux parsing, TTL caching, CLI & Web wiring."""
from __future__ import annotations

import json
from typing import Any

import pytest

from codelet.llm.models import (
    ModelCatalog,
    ModelInfo,
    models_endpoint,
    normalize_base_url,
    parse_models_payload,
    resolve_ttl_seconds,
)


ZENMUX_SAMPLE = {
    "object": "list",
    "data": [
        {
            "id": "anthropic/claude-sonnet-4.5",
            "object": "model",
            "display_name": "Anthropic: Claude Sonnet 4.5",
            "created": 1759196009,
            "owned_by": "anthropic",
            "input_modalities": ["text", "image", "file"],
            "output_modalities": ["text"],
            "capabilities": {"reasoning": True},
            "context_length": 200000,
            "pricings": {
                "prompt": [
                    {"value": 6.0, "unit": "perMTokens", "currency": "USD", "conditions": {"min_tokens": 200000}},
                    {"value": 3.0, "unit": "perMTokens", "currency": "USD"},
                ],
                "completion": [
                    {"value": 15.0, "unit": "perMTokens", "currency": "USD"},
                ],
            },
        },
        {
            "id": "openai/gpt-6-luna",
            "object": "model",
            "display_name": "OpenAI: GPT-6 Luna",
            "created": 1762000000,
            "owned_by": "openai",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "context_length": 128000,
            "pricings": {
                "prompt": [{"value": 0.1, "unit": "perMTokens", "currency": "USD"}],
                "completion": [{"value": 0.5, "unit": "perMTokens", "currency": "USD"}],
            },
        },
        {
            # Pure image generation model -- should be filtered out for coding chat.
            "id": "openai/gpt-image-1",
            "object": "model",
            "display_name": "GPT Image 1",
            "owned_by": "openai",
            "input_modalities": ["text"],
            "output_modalities": ["image"],
            "pricings": {
                "image": [{"value": 0.04, "unit": "perCount", "currency": "USD"}],
            },
        },
    ],
}


def test_parse_zenmux_payload_extracts_metadata_and_filters_non_text():
    models = parse_models_payload(ZENMUX_SAMPLE)
    ids = [m.id for m in models]
    assert "anthropic/claude-sonnet-4.5" in ids
    assert "openai/gpt-6-luna" in ids
    assert "openai/gpt-image-1" not in ids  # output_modalities=["image"] excluded

    sonnet = next(m for m in models if m.id == "anthropic/claude-sonnet-4.5")
    assert sonnet.display_name == "Anthropic: Claude Sonnet 4.5"
    assert sonnet.owned_by == "anthropic"
    assert sonnet.context_length == 200000
    assert sonnet.vision is True
    assert sonnet.reasoning is True
    # Unconditional tier (3.0) preferred over conditional long-context tier (6.0).
    assert sonnet.pricing == {"input": 3.0, "output": 15.0}

    luna = next(m for m in models if m.id == "openai/gpt-6-luna")
    assert luna.vision is False
    assert luna.reasoning is False
    assert luna.pricing == {"input": 0.1, "output": 0.5}


def test_parse_minimal_openai_and_openrouter_pricing():
    payload = {
        "object": "list",
        "data": [
            {"id": "deepseek-chat"},
            {
                "id": "google/gemini-2.5-pro",
                "context_length": 1000000,
                "pricing": {"prompt": "0.00000125", "completion": "0.00001"},
            },
        ],
    }
    models = parse_models_payload(payload)
    by_id = {m.id: m for m in models}
    assert "deepseek-chat" in by_id
    assert by_id["deepseek-chat"].pricing is None
    assert by_id["google/gemini-2.5-pro"].owned_by == "google"
    assert by_id["google/gemini-2.5-pro"].pricing == {"input": 1.25, "output": 10.0}


def test_endpoint_normalization():
    assert normalize_base_url("https://zenmux.ai/api/v1/") == "https://zenmux.ai/api/v1"
    assert normalize_base_url("https://zenmux.ai/api/v1/models") == "https://zenmux.ai/api/v1"
    assert models_endpoint("https://zenmux.ai/api/v1") == "https://zenmux.ai/api/v1/models"
    assert models_endpoint(None, provider="openai") == ""
    assert models_endpoint(None, provider="anthropic") == "https://api.anthropic.com/v1/models"


def test_catalog_ttl_cache_refresh_and_stale_fallback(tmp_path):
    now = [1000.0]
    calls: list[tuple[str | None, str | None]] = []
    should_fail = [False]

    def fake_fetch(base_url, api_key, *, provider="openai"):
        calls.append((base_url, api_key))
        if should_fail[0]:
            raise RuntimeError("network down")
        return parse_models_payload(ZENMUX_SAMPLE)

    cache_file = tmp_path / "models_cache.json"
    cat = ModelCatalog(cache_path=cache_file, ttl_seconds=3600, fetcher=fake_fetch, clock=lambda: now[0])

    # Cold start -> fetches from remote and writes atomic cache.
    assert cat.needs_refresh("https://zenmux.ai/api/v1") is True
    s1 = cat.refresh("https://zenmux.ai/api/v1", "sk-test")
    assert s1.source == "remote" and len(s1.models) == 2
    assert len(calls) == 1
    assert cache_file.is_file() and not list(tmp_path.glob("*.tmp"))

    # Within TTL -> served from disk cache, zero network calls.
    now[0] += 1800.0
    assert cat.needs_refresh("https://zenmux.ai/api/v1") is False
    s2 = cat.refresh("https://zenmux.ai/api/v1", "sk-test")
    assert s2.source == "cache" and len(s2.models) == 2
    assert len(calls) == 1

    # Past TTL with network error -> falls back to stale cache without crashing.
    now[0] += 3600.0
    assert cat.needs_refresh("https://zenmux.ai/api/v1") is True
    should_fail[0] = True
    s3 = cat.refresh("https://zenmux.ai/api/v1", "sk-test")
    assert s3.source == "stale_cache" and len(s3.models) == 2
    assert s3.error and "network down" in s3.error
    assert len(calls) == 2

    # Force refresh once network recovers -> remote update succeeds.
    should_fail[0] = False
    s4 = cat.refresh("https://zenmux.ai/api/v1", "sk-test", force=True)
    assert s4.source == "remote" and s4.error is None
    assert len(calls) == 3


def test_resolve_ttl_precedence(monkeypatch):
    monkeypatch.delenv("LLM_MODELS_TTL", raising=False)
    assert resolve_ttl_seconds({"models_cache_ttl_seconds": 7200}) == 7200
    monkeypatch.setenv("LLM_MODELS_TTL", "600")
    assert resolve_ttl_seconds({"models_cache_ttl_seconds": 7200}) == 600


def test_cli_models_and_model_switch(tmp_path, monkeypatch):
    from rich.console import Console
    from codelet.agent_loop import AgentLoop
    from codelet.config import Config, LLMProvider
    from codelet.llm.base import LLMClient, LLMResponse
    from codelet.cli import _handle_slash
    from codelet.slash.loader import SlashCommandIndex

    class _StubClient(LLMClient):
        def chat(self, **kwargs: Any) -> LLMResponse:
            return LLMResponse(text_blocks=["ok"], raw_content=[{"type": "text", "text": "ok"}], stop_reason="end_turn")

    cache_file = tmp_path / "models_cache.json"
    monkeypatch.setattr("codelet.llm.models.default_cache_path", lambda: cache_file)
    monkeypatch.setattr("codelet.llm.models.fetch_models", lambda *a, **kw: parse_models_payload(ZENMUX_SAMPLE))

    cfg = Config(provider=LLMProvider.OPENAI, base_url="https://zenmux.ai/api/v1",
                 api_key="sk-x", model="openai/gpt-6-luna")
    buf_console = Console(record=True, width=120)
    agent = AgentLoop(config=cfg, client=_StubClient(), console=buf_console)

    cont, prompt = _handle_slash("/models refresh", agent, buf_console, SlashCommandIndex(), None)
    assert cont is True and prompt is None
    text = buf_console.export_text()
    assert "anthropic/claude-sonnet-4.5" in text
    assert "openai/gpt-6-luna" in text

    # Switching model via /model applies catalog context_length and pricing.
    cont, prompt = _handle_slash("/model anthropic/claude-sonnet-4.5", agent, buf_console, SlashCommandIndex(), None)
    assert cont is True and prompt is None
    assert agent.config.model == "anthropic/claude-sonnet-4.5"
    assert agent.config.context_window == 200000
    assert agent.telemetry.pricing["anthropic/claude-sonnet-4.5"] == {"input": 3.0, "output": 15.0}


def test_web_models_api_refresh_and_ws_broadcast(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from codelet.llm.base import LLMClient, LLMResponse
    from codelet.web.server import create_app

    class _StubClient(LLMClient):
        def chat(self, **kwargs: Any) -> LLMResponse:
            return LLMResponse(text_blocks=["ok"], raw_content=[{"type": "text", "text": "ok"}], stop_reason="end_turn")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codelet.web.server.load_env_files", lambda *a, **kw: [])
    monkeypatch.setattr("codelet.agent_loop.build_client", lambda config: _StubClient())
    monkeypatch.setenv("LLM_BASE_URL", "https://zenmux.ai/api/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-6-luna")
    monkeypatch.delenv("LLM_MODELS", raising=False)

    fetch_count = [0]

    async def fake_afetch(base_url, api_key, *, provider="openai"):
        fetch_count[0] += 1
        return parse_models_payload(ZENMUX_SAMPLE)

    catalog = ModelCatalog(cache_path=tmp_path / "models_cache.json", ttl_seconds=3600, afetcher=fake_afetch)
    app = create_app(catalog=catalog, auto_refresh=False)
    client = TestClient(app)

    # Before refresh: empty cache -> fallback to active model only.
    r0 = client.get("/api/models").json()
    assert r0["model_ids"] == ["openai/gpt-6-luna"]
    assert r0["meta"]["source"] == "fallback"

    # Manual POST /api/models/refresh pulls full list and populates cache.
    r1 = client.post("/api/models/refresh").json()
    assert fetch_count[0] == 1
    assert set(r1["model_ids"]) == {"anthropic/claude-sonnet-4.5", "openai/gpt-6-luna"}
    assert r1["meta"]["source"] == "remote"

    # WebSocket receives model_catalog and can refresh + switch models live.
    with client.websocket_connect("/ws") as ws:
        prof = ws.receive_json()
        assert prof["type"] == "profile"
        assert "anthropic/claude-sonnet-4.5" in prof["models"]
        assert any(m["id"] == "anthropic/claude-sonnet-4.5" and m["vision"] for m in prof["model_catalog"])

        ws.send_json({"type": "refresh_models"})
        got_prof = None
        for _ in range(10):
            f = ws.receive_json()
            if f["type"] == "profile":
                got_prof = f
                break
        assert got_prof is not None and fetch_count[0] == 2

        ws.send_json({"type": "set_model", "model": "anthropic/claude-sonnet-4.5"})
        switched = None
        for _ in range(10):
            f = ws.receive_json()
            if f["type"] == "profile":
                switched = f
                break
        assert switched is not None and switched["model"] == "anthropic/claude-sonnet-4.5"


def test_web_auto_refresh_on_connect_populates_stale_catalog(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from codelet.llm.base import LLMClient, LLMResponse
    from codelet.web.server import create_app

    class _StubClient(LLMClient):
        def chat(self, **kwargs: Any) -> LLMResponse:
            return LLMResponse(text_blocks=["ok"], raw_content=[{"type": "text", "text": "ok"}], stop_reason="end_turn")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codelet.web.server.load_env_files", lambda *a, **kw: [])
    monkeypatch.setattr("codelet.agent_loop.build_client", lambda config: _StubClient())
    monkeypatch.setenv("LLM_BASE_URL", "https://zenmux.ai/api/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-6-luna")
    monkeypatch.delenv("LLM_MODELS", raising=False)

    async def fake_afetch(base_url, api_key, *, provider="openai"):
        return parse_models_payload(ZENMUX_SAMPLE)

    catalog = ModelCatalog(cache_path=tmp_path / "models_cache.json", ttl_seconds=3600, afetcher=fake_afetch)
    app = create_app(catalog=catalog, auto_refresh=True)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            seen_models: set[str] = set()
            for _ in range(12):
                f = ws.receive_json()
                if f["type"] == "profile":
                    seen_models.update(f["models"])
                    if "anthropic/claude-sonnet-4.5" in seen_models:
                        break
            assert "anthropic/claude-sonnet-4.5" in seen_models
