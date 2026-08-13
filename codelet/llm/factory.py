"""LLM client factory.

Picks a concrete `LLMClient` based on `Config.provider`. The (provider, model,
base_url, api_key) quartet is supplied via Config; profile resolution happens
upstream in `cli._build_config` / `settings.resolve_profile`.
"""
from __future__ import annotations

from ..config import Config, LLMProvider
from .base import LLMClient


def build_client(config: Config) -> LLMClient:
    if config.provider == LLMProvider.ANTHROPIC:
        from .anthropic_client import AnthropicClient
        return AnthropicClient(api_key=config.api_key, base_url=config.base_url)

    if config.provider == LLMProvider.OPENAI:
        from .openai_compat import OpenAICompatClient
        return OpenAICompatClient(api_key=config.api_key, base_url=config.base_url)

    raise ValueError(f"Unknown provider: {config.provider}")
