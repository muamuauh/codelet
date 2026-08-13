"""Configuration -- single source of runtime settings.

P1 keeps this minimal (mirrors original). Later phases extend with settings.json
loader, hooks, and skill paths.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PermissionMode(Enum):
    ASK = "ask"
    AUTO = "auto"
    PLAN = "plan"


class LLMProvider(Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"  # OpenAI-compatible (DeepSeek/Ollama/SiliconFlow); P5


@dataclass
class Config:
    # Model + provider (the (provider, model, base_url, api_key) quartet)
    provider: LLMProvider = LLMProvider.ANTHROPIC
    model: str = "claude-sonnet-4-5"
    base_url: str | None = None
    api_key: str | None = None
    profile_name: str | None = None  # diagnostic; what profile produced this config

    # Loop
    max_turns: int = 30
    max_tokens: int = 8192

    # Streaming: print assistant text token-by-token + show a spinner while
    # waiting for the first token. Auto-disabled for non-TTY output and for
    # subagents (their text isn't shown live). Toggle with --no-stream.
    stream: bool = True

    # Context
    max_context_messages: int = 100
    max_output_chars: int = 50_000

    # Compaction (P4)
    context_window: int = 200_000               # tokens; sonnet-4-5 default
    compact_threshold_ratio: float = 0.75       # trigger when est tokens > ratio * window
    compact_keep_recent: int = 4                # last N messages always preserved
    compact_model: str = "claude-haiku-4-5"     # cheap summarizer
    compact_summary_target_tokens: int = 500    # ~tokens for the produced summary

    # Hooks (P4): keyed by event name -> list of {"matcher": "...", "command": "..."}
    hooks: dict[str, list[dict[str, str]]] = field(default_factory=dict)

    # Telemetry pricing overrides (USD per million tokens)
    pricing_overrides: dict[str, dict[str, float]] = field(default_factory=dict)

    # Permissions
    permission_mode: PermissionMode = PermissionMode.ASK
    allowed_commands: list[str] = field(default_factory=lambda: [
        "ls", "dir", "cat", "head", "tail", "wc", "find", "grep", "rg",
        "git status", "git diff", "git log", "git branch",
        "python", "python3", "pip", "npm", "node",
        "echo", "pwd", "which", "where", "env", "date",
    ])
    denied_patterns: list[str] = field(default_factory=lambda: [
        "rm -rf /", "rm -rf ~", "sudo rm",
        "git push --force", "git reset --hard",
        "> /dev/sda", "mkfs", "dd if=",
        ":(){ :|:& };:",
    ])
