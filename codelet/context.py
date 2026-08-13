"""Conversation context.

P1-P3: in-memory message list with naive truncation + CLAUDE.md loading.
P4 adds token-budget compaction: when estimated tokens exceed
`compact_threshold_ratio * context_window`, the middle of the conversation is
summarized via a cheap Haiku call and replaced with a single user message
holding a `<conversation_summary>` block.

Compaction safety rules:
  - The first message (the seed user prompt) is always preserved verbatim.
  - The last `compact_keep_recent` messages are always preserved verbatim,
    so any in-flight assistant tool_use / user tool_result pairing stays
    intact. (Anthropic API requires a tool_use to be followed by its
    matching tool_result before the next assistant turn.)
  - The middle slice is replaced with a single user message containing a
    summary block. If the slice is empty, nothing happens.

Stores Anthropic-shaped messages: list of dicts where content is either str or
a list of content blocks (text / tool_use / tool_result).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Config

if TYPE_CHECKING:
    from .llm.base import LLMClient


@dataclass
class ConversationContext:
    config: Config
    messages: list[dict[str, Any]] = field(default_factory=list)
    _system_prompt: str = ""
    depth: int = 0  # subagent recursion depth; 0 for the root agent
    compactions: int = 0

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})
        self._truncate_if_needed()

    def add_assistant_message(self, content: Any) -> None:
        self.messages.append({"role": "assistant", "content": content})
        self._truncate_if_needed()

    def add_tool_results(self, results: list[dict[str, Any]]) -> None:
        """Append a single user turn carrying multiple tool_result blocks (Anthropic format)."""
        if not results:
            return
        self.messages.append({"role": "user", "content": results})
        self._truncate_if_needed()

    def get_api_messages(self) -> list[dict[str, Any]]:
        return list(self.messages)

    def _truncate_if_needed(self) -> None:
        """Keep first message + last (max-1) messages.

        Acts as a hard ceiling backstop in case compaction never runs (e.g.
        no LLMClient passed in for unit tests). Real production trims happen
        via `compact_if_needed`.
        """
        max_msgs = self.config.max_context_messages
        if len(self.messages) > max_msgs:
            self.messages = self.messages[:1] + self.messages[-(max_msgs - 1):]

    # ---------- compaction ----------

    def estimate_tokens(self, client: "LLMClient | None" = None) -> int:
        """Rough total token count across stored messages.

        We render every message to a flat string (so tool_use/tool_result
        blocks contribute) and let the LLMClient's count_tokens estimate.
        Fast and stable -- accuracy doesn't need to beat 5%.
        """
        rendered = "\n".join(_render_message(m) for m in self.messages)
        if client is not None:
            return client.count_tokens(rendered)
        return max(1, len(rendered) // 4)

    def should_compact(self, client: "LLMClient | None" = None) -> bool:
        threshold = int(self.config.context_window * self.config.compact_threshold_ratio)
        return self.estimate_tokens(client) > threshold

    async def compact_if_needed(self, client: "LLMClient") -> bool:
        """If over threshold, replace the middle slice with a summary block.

        Returns True if compaction actually happened.
        """
        if not self.should_compact(client):
            return False

        keep_recent = max(2, self.config.compact_keep_recent)
        if len(self.messages) <= keep_recent + 1:
            # Not enough middle to compact away meaningfully.
            return False

        head = self.messages[:1]                        # seed user prompt
        middle = self.messages[1:-keep_recent]          # to be summarized
        tail = self.messages[-keep_recent:]             # preserved verbatim

        if not middle:
            return False

        summary_text = await _summarize(client, middle, self.config)
        summary_message = {
            "role": "user",
            "content": (
                "<conversation_summary>\n"
                f"{summary_text}\n"
                "</conversation_summary>"
            ),
        }

        self.messages = head + [summary_message] + tail
        self.compactions += 1
        return True


def _render_message(msg: dict[str, Any]) -> str:
    role = msg.get("role", "?")
    content = msg.get("content", "")
    if isinstance(content, str):
        return f"[{role}] {content}"
    parts: list[str] = [f"[{role}]"]
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            parts.append(f"<tool_use {block.get('name')} {block.get('input')}>")
        elif btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                inner = " ".join(b.get("text", "") for b in inner if isinstance(b, dict))
            parts.append(f"<tool_result {inner}>")
    return " ".join(parts)


async def _summarize(client: "LLMClient", middle: list[dict[str, Any]], config: Config) -> str:
    """Ask the LLM for a compact summary of the middle slice.

    Uses a cheap model (Haiku by default; configurable via
    `Config.compact_model`) so summarization stays inexpensive.
    """
    import asyncio

    rendered = "\n\n".join(_render_message(m) for m in middle)
    prompt = (
        "Summarize the following conversation chunk for context preservation. "
        "Keep concrete facts, decisions, file paths, and tool outputs that "
        "future turns might need. Drop greetings and meta-chatter. Aim for "
        f"about {config.compact_summary_target_tokens} tokens of plain text.\n\n"
        f"---\n{rendered}\n---"
    )
    response = await asyncio.to_thread(
        client.chat,
        messages=[{"role": "user", "content": prompt}],
        system="You are a context-summarization assistant. Output ONLY the summary, no preamble.",
        tools=[],
        model=config.compact_model,
        max_tokens=config.compact_summary_target_tokens * 4,  # rough cushion
    )
    text = "\n".join(response.text_blocks).strip()
    return text or "(empty summary returned)"


def load_project_instructions(project_dir: str | Path | None = None) -> str:
    """Load CLAUDE.md from the project root (if present)."""
    if project_dir is None:
        project_dir = Path.cwd()
    else:
        project_dir = Path(project_dir)

    claude_md = project_dir / "CLAUDE.md"
    if claude_md.exists() and claude_md.is_file():
        return claude_md.read_text(errors="replace").strip()
    return ""
