"""Conversation context.

P1-P3: in-memory message list with naive truncation + CLAUDE.md loading.
P4 adds token-budget compaction; it is now layered, cheapest layer first:

  L0  A single tool output longer than `max_output_chars` is capped at write
      time (head + tail kept, full text spilled to a temp file). Lives in
      agent_loop, where results are produced.
  L1  Past `compact_clear_ratio * context_window`, old tool outputs longer than
      `compact_clear_min_chars` are replaced by a one-line placeholder. No LLM
      call; the tool_use / tool_result pairing is untouched. The model already
      acted on those outputs, and can re-run the tool if it needs one again.
  L2  Past `compact_threshold_ratio * context_window` (or the message-count
      trigger below), the middle of the conversation is summarized by
      `compact_model` into a structured `<conversation_summary>`. The summary
      is rolling: an earlier summary in the middle is merged, not re-summarized
      as prose. State the caller pins (the todo list) is re-attached verbatim.
  L3  `max_context_messages` is the hard ceiling (`_truncate_if_needed`).

Token budget: the estimate is anchored on the `input_tokens` the provider
reported for the last call (system prompt + tools + messages, counted by the
real tokenizer), plus a local heuristic for messages appended since. Without an
anchor it falls back to the heuristic alone. No count_tokens round-trip.

Compaction safety rules:
  - The first message (the seed user prompt) is always preserved verbatim.
  - The last `compact_keep_recent` messages are always preserved verbatim,
    so any in-flight assistant tool_use / user tool_result pairing stays
    intact. (Anthropic API requires a tool_use to be followed by its
    matching tool_result before the next assistant turn.) If that slice
    would open on a tool_result, it is widened by one to take its tool_use.
  - The middle slice is replaced with a single user message containing a
    summary block. If the slice is empty, nothing happens.
  - L2 also fires on message count (`compact_threshold_ratio *
    max_context_messages`), so a long run of small tool calls is summarized
    before the hard `max_context_messages` ceiling drops the middle unread.

The hard ceiling obeys the same pairing rule: a kept tail never opens on a
tool_result whose tool_use was cut off (the API rejects that with a 400).

Stores Anthropic-shaped messages: list of dicts where content is either str or
a list of content blocks (text / tool_use / tool_result).
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Config

if TYPE_CHECKING:
    from .llm.base import LLMClient

SUMMARY_OPEN, SUMMARY_CLOSE = "<conversation_summary>", "</conversation_summary>"
CLEARED_PREFIX = "[cleared to save context:"
# Written by agent_loop's L0 cap; L1 reuses that file instead of writing another.
SPILL_RE = re.compile(r"full output saved to (.+?) \.\.\.\]")
# Where L0 and L1 put tool outputs they take out of the context. Outside the
# workspace on purpose: a file under the repo would show up in `git status`
# and in SWE-bench patches.
SPILL_DIR = Path(tempfile.gettempdir()) / "codelet-tool-outputs"
_CJK_RE = re.compile(r"[　-鿿가-힯＀-￯]")


def spill(tool_use_id: str, text: str) -> Path | None:
    """Save a tool output the context is dropping, so the agent can read it back
    with read_file instead of re-running the tool. None if it could not be saved."""
    path = SPILL_DIR / (re.sub(r"[^A-Za-z0-9_-]", "_", tool_use_id) + ".txt")
    try:
        SPILL_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return path


def approx_tokens(text: str) -> int:
    """Tokenizer-free estimate: ~1 token per CJK character, ~4 chars per token
    otherwise. `len // 4` alone undercounts Chinese about fourfold, which made
    compaction fire far too late on Chinese sessions."""
    cjk = len(_CJK_RE.findall(text))
    return max(1, cjk + (len(text) - cjk) // 4)


@dataclass
class ConversationContext:
    config: Config
    messages: list[dict[str, Any]] = field(default_factory=list)
    _system_prompt: str = ""
    depth: int = 0  # subagent recursion depth; 0 for the root agent
    compactions: int = 0        # L2 summaries
    cleared_results: int = 0    # L1 tool outputs replaced by a placeholder
    # (input_tokens the provider reported, len(messages) sent on that call)
    _anchor: tuple[int, int] | None = None

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

        The cut skips forward past any tool_result turn at the start of the
        kept tail: its tool_use is in the dropped part, and an orphaned
        tool_result makes the next API call fail. So the result can be a
        message or two under the ceiling, never over it.
        """
        max_msgs = self.config.max_context_messages
        if len(self.messages) > max_msgs:
            start = len(self.messages) - (max_msgs - 1)
            while start < len(self.messages) and _is_tool_result_turn(self.messages[start]):
                start += 1
            self.messages = self.messages[:1] + self.messages[start:]
            self._anchor = None

    # ---------- token budget ----------

    def note_usage(self, usage: dict[str, int]) -> None:
        """Anchor the estimate on the provider's count for the call that just
        returned. Called before that call's reply is appended, so it covers
        exactly the messages that were sent."""
        n = int(usage.get("input_tokens") or 0)
        if n > 0:
            self._anchor = (n, len(self.messages))

    def estimate_tokens(self) -> int:
        """Prompt size the next call will have: the last reported
        `input_tokens` plus a heuristic for whatever was appended since.
        Falls back to the heuristic over everything when there is no anchor
        (first call, or the message list was rewritten)."""
        if self._anchor is not None and self._anchor[1] <= len(self.messages):
            tokens, sent = self._anchor
            return tokens + sum(approx_tokens(_render_message(m)) for m in self.messages[sent:])
        return approx_tokens(self._system_prompt) + sum(
            approx_tokens(_render_message(m)) for m in self.messages)

    def should_compact(self) -> bool:
        """Would L2 (summary) fire now."""
        ratio = self.config.compact_threshold_ratio
        # Count first: it is free, and it catches the case the token check
        # misses -- many small tool rounds that reach the message ceiling
        # while still far below the token threshold.
        if len(self.messages) >= int(self.config.max_context_messages * ratio):
            return True
        return self.estimate_tokens() > int(self.config.context_window * ratio)

    # ---------- compaction ----------

    async def compact_if_needed(self, client: "LLMClient", pinned: str = "") -> bool:
        """Run the cheapest layer that brings the context back under budget.

        L1 (clear old tool outputs) runs first and costs nothing; L2 (LLM
        summary) runs only if L1 was not enough, or on the message-count
        trigger, which L1 cannot help with. `pinned` is caller state to
        re-attach after a summary (the todo list), so compaction cannot make
        the agent lose its plan. Returns True if anything was compacted.
        """
        ratio = self.config.compact_threshold_ratio
        count_hit = len(self.messages) >= int(self.config.max_context_messages * ratio)
        clear_at = int(self.config.context_window * min(self.config.compact_clear_ratio, ratio))

        did = False
        if not count_hit and self.estimate_tokens() > clear_at:
            n = self._clear_old_tool_results(self._tail_start())
            self.cleared_results += n
            did = n > 0

        if not self.should_compact():
            return did

        keep_recent = max(2, self.config.compact_keep_recent)
        if len(self.messages) <= keep_recent + 1:
            # Not enough middle to compact away meaningfully.
            return did

        tail_start = self._tail_start()
        head = self.messages[:1]                        # seed user prompt
        middle = self.messages[1:tail_start]            # to be summarized
        tail = self.messages[tail_start:]               # preserved verbatim

        if not middle:
            return did

        previous = "\n\n".join(_summary_body(m) for m in middle if _is_summary(m))
        events = [m for m in middle if not _is_summary(m)]
        summary_text = await _summarize(client, events, self.config, previous=previous)
        content = f"{SUMMARY_OPEN}\n{summary_text}\n{SUMMARY_CLOSE}"
        if pinned:
            content += f"\n\n<current_state>\n{pinned}\n</current_state>"

        self.messages = head + [{"role": "user", "content": content}] + tail
        self.compactions += 1
        self._anchor = None
        return True

    def _tail_start(self) -> int:
        """Index where the always-kept tail begins, never on a tool_result."""
        start = max(1, len(self.messages) - max(2, self.config.compact_keep_recent))
        if start > 1 and _is_tool_result_turn(self.messages[start]):
            start -= 1                                  # keep the tool_use with its result
        return start

    def _clear_old_tool_results(self, stop: int) -> int:
        """L1: replace long tool outputs before `stop` with a placeholder that
        names the tool, the size, and a spill file holding the full text (L0's
        if it wrote one). Re-running the tool is not always possible: a log
        rotates, a page changes, a command is not idempotent."""
        names: dict[str, str] = {}
        cleared = 0
        for msg in self.messages[:stop]:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for i, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    names[block.get("id", "")] = block.get("name", "tool")
                    continue
                text = block.get("content")
                if (block.get("type") != "tool_result" or not isinstance(text, str)
                        or len(text) <= self.config.compact_clear_min_chars
                        or text.startswith(CLEARED_PREFIX)):
                    continue
                found = SPILL_RE.search(text)
                path = found.group(1) if found else spill(block.get("tool_use_id", "out"), text)
                where = (f"full output: {path}" if path
                         else "re-run the tool if you need it again")
                name = names.get(block.get("tool_use_id", ""), "tool")
                content[i] = {**block, "content": f"{CLEARED_PREFIX} {name} output, "
                                                  f"{len(text)} chars; {where}]"}
                cleared += 1
        if cleared:
            self._anchor = None
        return cleared


def _is_tool_result_turn(msg: dict[str, Any]) -> bool:
    """A user turn carrying tool_result blocks -- only valid right after its tool_use."""
    content = msg.get("content")
    return (msg.get("role") == "user" and isinstance(content, list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content))


def _is_summary(msg: dict[str, Any]) -> bool:
    content = msg.get("content")
    return msg.get("role") == "user" and isinstance(content, str) and content.startswith(SUMMARY_OPEN)


def _summary_body(msg: dict[str, Any]) -> str:
    """Just the summary text -- the pinned state after it is re-attached fresh."""
    text = msg["content"][len(SUMMARY_OPEN):]
    return text.split(SUMMARY_CLOSE, 1)[0].strip()


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


SUMMARY_SECTIONS = ("Goal", "User instructions", "Decisions", "Facts", "Files", "Progress", "Errors")

# The rules live in the system prompt: written into the user turn next to the
# events, the summarizer copied them into the summary as if the user had said
# them. Instructions and decisions are separate sections because, sharing one,
# the user's instructions crowded out the agent's own choices (a rename was the
# fact most often lost). Facts exists because a value that was only ever in a
# tool output had no section to go in. See evals/compaction/retention.py.
_SUMMARY_SYSTEM = """\
You compress the middle of a coding agent's conversation so the agent can keep
working without it. Output only the summary, in exactly these sections, in this
order, as terse bullet points ("none" if a section is empty):

## Goal -- what the user wants, including any later change of mind
## User instructions -- every explicit instruction or constraint the user gave
## Decisions -- every choice the agent or the user made, with the exact name or value chosen (renames, libraries, approaches)
## Facts -- concrete values seen in tool outputs that a later step may need: ids, versions, config values, owners, test names, error text
## Files -- every path read, created or modified, and its current state
## Progress -- steps done, and the next step
## Errors -- failures and dead ends, so they are not retried

Copy identifiers and values exactly as written: paths, names, numbers, commands.
Never replace a concrete value with a description of it ("a build id", "the
ownership structure"). Skip noise such as timestamps and routine log lines.
These rules are for you; do not repeat them in the summary. Aim for about
{target} tokens."""

_MERGE = ("An earlier summary of even older history comes first. Merge it with the "
          "new events: keep every item from it that is still true, update what "
          "changed, and drop nothing merely because it is old.\n\n")


async def _summarize(client: "LLMClient", middle: list[dict[str, Any]], config: Config,
                     previous: str = "") -> str:
    """L2: structured, rolling summary of the middle slice.

    Uses `config.compact_model` (a cheap model by default; per profile, see
    cli._build_config) so summarization stays inexpensive.
    """
    import asyncio

    events = "\n\n".join(_render_message(m) for m in middle)
    prompt = (f"{_MERGE}<previous_summary>\n{previous}\n</previous_summary>\n\n" if previous else "") \
        + f"<events>\n{events}\n</events>"
    response = await asyncio.to_thread(
        client.chat,
        messages=[{"role": "user", "content": prompt}],
        system=_SUMMARY_SYSTEM.format(target=config.compact_summary_target_tokens),
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
