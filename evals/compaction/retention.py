"""Compaction retention eval.

The question: after the context is compacted, can the agent still answer
questions about facts that appeared early in the session? A unit test can pin
the mechanics (pairing, layering, rolling); only a real summarizer can say what
a summary keeps.

Each scenario is a synthetic coding session, built deterministically from a
seed, with facts planted in its first three quarters (so they fall in the
compacted middle, never in the preserved tail). Four kinds, because they fail differently:

  user        the user stated it ("use port 5317 for the test DB")
  decision    the agent decided it ("I'll rename parse_cfg to load_config_42")
  tool_ack    it was in a tool output AND the agent restated it
  tool_only   it was only ever in a tool output (e.g. a build id in a CI log)

Two families: `tools` (most tokens are bulky tool outputs -- what L1 clears)
and `chat` (most tokens are prose -- what only L2 can shrink).

Strategies, all applied to the same session:

  full        no compaction -- the upper bound; checks the questions are answerable
  baseline    the pre-P1 compaction: one free-prose summary of ~500 tokens
  structured  the new L2 alone (seven-section rolling summary), L1 disabled
  layered     the shipped default: L1 clears old tool outputs, L2 only if still over

A reader model then answers every planted question from the compacted context
(rendered as a transcript, so any provider can read it). A fact counts as
retained when the expected value appears in the answer. The reader cannot open
files, so a fact L1 moved into a spill file is reported separately as
recoverable: the agent itself could `read_file` the path the placeholder names.

Usage:
    python -m evals.compaction.retention --compact-model claude-haiku-4-5 --reader-model claude-haiku-4-5
    python -m evals.compaction.retention --scenarios 2 --strategies full,layered
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from codelet.cli import _build_config
from codelet.config import Config
from codelet.context import ConversationContext, _is_tool_result_turn, _render_message, approx_tokens
from codelet.llm.base import LLMClient
from codelet.llm.factory import build_client
from codelet.settings import load_env_files, load_settings

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
KINDS = ("user", "decision", "tool_ack", "tool_only")
STRATEGIES = ("full", "baseline", "structured", "layered")

# The pre-P1 summarizer prompt, verbatim, so "baseline" is what actually shipped.
OLD_PROMPT = (
    "Summarize the following conversation chunk for context preservation. "
    "Keep concrete facts, decisions, file paths, and tool outputs that "
    "future turns might need. Drop greetings and meta-chatter. Aim for "
    "about {target} tokens of plain text.\n\n---\n{rendered}\n---"
)
OLD_SYSTEM = "You are a context-summarization assistant. Output ONLY the summary, no preamble."


# ---------- scenarios ----------

@dataclass
class Fact:
    key: str
    kind: str
    question: str
    answer: str


@dataclass
class Scenario:
    name: str
    family: str
    messages: list[dict[str, Any]]
    facts: list[Fact] = field(default_factory=list)


class _Session:
    """Appends well-formed Anthropic-shaped turns: every tool_use gets its result,
    and an agent's remark rides in the same assistant turn as its next tool call."""

    def __init__(self, seed_prompt: str) -> None:
        self.messages: list[dict[str, Any]] = [{"role": "user", "content": seed_prompt}]
        self._n = 0
        self._remark: str | None = None

    def remark(self, text: str) -> None:
        self._remark = text

    def tool(self, name: str, tool_input: dict[str, Any], output: str) -> None:
        self._n += 1
        blocks: list[dict[str, Any]] = []
        if self._remark:
            blocks.append({"type": "text", "text": self._remark})
            self._remark = None
        blocks.append({"type": "tool_use", "id": f"toolu_{self._n:03d}", "name": name, "input": tool_input})
        self.messages.append({"role": "assistant", "content": blocks})
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{self._n:03d}", "content": output}]})

    def user_says(self, text: str) -> None:
        """A new user instruction arrives after the agent finishes a step."""
        self.messages.append({"role": "assistant", "content": [
            {"type": "text", "text": self._remark or "Done with that step."}]})
        self._remark = None
        self.messages.append({"role": "user", "content": text})


_WORDS = ("request", "handler", "cache", "retry", "token", "session", "worker", "queue",
          "config", "loader", "parser", "schema", "timeout", "socket", "buffer", "index")


def _log(rng: random.Random, lines: int) -> str:
    levels = ("DEBUG", "INFO", "INFO", "INFO", "WARN")
    return "\n".join(
        f"2026-09-2{rng.randint(0, 3)} 1{rng.randint(0, 9)}:{rng.randint(10, 59)}:{rng.randint(10, 59)} "
        f"{rng.choice(levels):5} {rng.choice(_WORDS)}.{rng.choice(_WORDS)}: "
        + " ".join(rng.choice(_WORDS) for _ in range(rng.randint(6, 14)))
        for _ in range(lines))


def _code(rng: random.Random, lines: int) -> str:
    out = []
    for i in range(lines):
        a, b = rng.choice(_WORDS), rng.choice(_WORDS)
        out.append(f"{i + 1:>5}|    {a}_{b} = {rng.choice(_WORDS)}.get_{b}({rng.randint(0, 99)})")
    return "\n".join(out)


def _prose(rng: random.Random, sentences: int) -> str:
    return " ".join(
        f"The {rng.choice(_WORDS)} {rng.choice(_WORDS)} path should "
        f"{rng.choice(('stay', 'become', 'remain', 'look'))} "
        f"{rng.choice(('simple', 'explicit', 'lazy', 'bounded', 'idempotent'))} because the "
        f"{rng.choice(_WORDS)} {rng.choice(('can', 'may', 'will'))} "
        f"{rng.choice(('retry', 'block', 'overflow', 'race', 'drift'))} under load."
        for _ in range(sentences))


def build_scenario(seed: int, family: str) -> Scenario:
    rng = random.Random(seed)
    port, build = rng.randint(5100, 5999), rng.randint(10_000, 99_999)
    py = rng.choice(("3.9", "3.10", "3.11"))
    frozen = f"legacy/billing_{rng.randint(10, 99)}.py"
    branch = f"feat/cfg-loader-{rng.randint(100, 999)}"
    new_name = f"load_config_{rng.randint(10, 99)}"
    lib = f"netkit{rng.randint(2, 9)}"
    retry = rng.randint(101, 199)       # 3 digits: never collides with log timestamps
    failing = f"test_{rng.choice(_WORDS)}_{rng.randint(10, 99)}"
    owner = f"@dev-{rng.choice(('lin', 'ana', 'omar', 'kai', 'zoe'))}{rng.randint(1, 9)}"
    ticket = f"ORION-{rng.randint(1000, 9999)}"

    facts = [
        Fact("db_port", "user", "Which port must the test database use?", str(port)),
        Fact("frozen_file", "user", "Which file must never be modified?", frozen),
        Fact("py_version", "user", "Which Python version must the code stay compatible with?", py),
        Fact("branch", "user", "Which git branch should the work go on?", branch),
        Fact("rename", "decision", "What new name did the agent choose for parse_cfg?", new_name),
        Fact("http_lib", "decision", "Which HTTP client library did the agent decide to use?", lib),
        Fact("retry", "tool_ack", "What is RETRY_LIMIT set to in settings.py?", str(retry)),
        Fact("failing", "tool_ack", "Which test file was failing when the work started?", failing),
        Fact("build_id", "tool_only", "What build id did the CI log report?", str(build)),
        Fact("owner", "tool_only", "Who is the CODEOWNER for src/core/?", owner),
    ]

    s = _Session(f"In the repo 'orion', refactor the config loader and fix the failing tests. "
                 f"Tracking ticket: {ticket}.")
    # 28 tool rounds stay under the 75-message count trigger, so "layered" gets to
    # try L1 first; the chat family crosses it, as prose-heavy sessions do.
    rounds = 28
    bulky = (lambda: _log(rng, rng.randint(70, 110))) if family == "tools" else (lambda: _log(rng, 6))

    plants = {  # round -> how the fact enters the session; all by round 21 of 28
        1: lambda: s.tool("bash", {"command": "pytest -q"},
                          _log(rng, 30) + f"\nFAILED tests/{failing}.py::test_roundtrip - AssertionError\n"
                          + _log(rng, 10)),
        2: lambda: s.remark(f"{failing}.py::test_roundtrip is the failing test; starting there."),
        4: lambda: s.user_says(f"Use port {port} for the test database, not the default."),
        6: lambda: s.tool("read_file", {"path": "orion/settings.py"},
                          _code(rng, 40) + f"\n   41|RETRY_LIMIT = {retry}\n" + _code(rng, 20)),
        7: lambda: s.remark(f"settings.py sets RETRY_LIMIT to {retry}; the loader must keep it."),
        9: lambda: s.user_says(f"Do not modify {frozen} under any circumstances."),
        11: lambda: s.remark(f"I'll rename parse_cfg to {new_name} to match the module naming."),
        13: lambda: s.tool("bash", {"command": "cat ci/last_run.log"},
                           _log(rng, 60) + f"\nbuild id: {build}\n" + _log(rng, 40)),
        15: lambda: s.user_says(f"Everything must stay compatible with Python {py}."),
        17: lambda: s.tool("read_file", {"path": "CODEOWNERS"},
                           f"docs/ @writers\nsrc/core/ {owner}\nsrc/web/ @frontend\n"),
        19: lambda: s.remark(f"I'll use {lib} for the HTTP client since it supports async."),
        21: lambda: s.user_says(f"Put this work on the branch {branch}."),
    }
    for r in range(rounds):
        if r in plants:
            plants[r]()
        if family == "chat" and r % 2 == 0:
            s.user_says(f"Also consider this: {_prose(rng, 18)}")
            s.remark(f"Understood. {_prose(rng, 14)}")
        s.tool(rng.choice(("grep", "bash", "read_file")),
               {"pattern": rng.choice(_WORDS)}, bulky())
    return Scenario(f"{family}-{seed}", family, s.messages, facts)


# ---------- strategies ----------

@dataclass
class Outcome:
    messages: list[dict[str, Any]]
    summarizer_input_tokens: int = 0
    summarizer_calls: int = 0
    cleared: int = 0
    summaries: int = 0
    summary: str = ""


class _CountingClient(LLMClient):
    """Passes calls through and tallies the summarizer's usage."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner, self.input_tokens, self.calls = inner, 0, 0

    def chat(self, **kwargs: Any):
        response = self.inner.chat(**kwargs)
        self.calls += 1
        self.input_tokens += int(response.usage.get("input_tokens", 0) or 0)
        return response


async def run_strategy(name: str, sc: Scenario, client: LLMClient, base: Config) -> Outcome:
    msgs = copy.deepcopy(sc.messages)
    counting = _CountingClient(client)
    if name == "full":
        return Outcome(msgs)
    if name == "baseline":
        keep = max(2, base.compact_keep_recent)
        start = len(msgs) - keep
        if _is_tool_result_turn(msgs[start]):
            start -= 1   # same pairing fix as shipped, so only the summary differs
        rendered = "\n\n".join(_render_message(m) for m in msgs[1:start])
        response = await asyncio.to_thread(
            counting.chat, messages=[{"role": "user", "content": OLD_PROMPT.format(target=500, rendered=rendered)}],
            system=OLD_SYSTEM, tools=[], model=base.compact_model, max_tokens=2_000)
        summary = "\n".join(response.text_blocks).strip()
        msgs = msgs[:1] + [{"role": "user", "content":
                            f"<conversation_summary>\n{summary}\n</conversation_summary>"}] + msgs[start:]
        return Outcome(msgs, counting.input_tokens, counting.calls, summary=summary, summaries=1)

    cfg = copy.copy(base)
    if name == "structured":
        cfg.compact_clear_min_chars = 10 ** 9      # L1 never qualifies: isolate the summary
    ctx = ConversationContext(config=cfg, messages=msgs)
    await ctx.compact_if_needed(counting)
    summary = next((m["content"] for m in ctx.messages if isinstance(m["content"], str)
                    and m["content"].startswith("<conversation_summary>")), "")
    return Outcome(ctx.messages, counting.input_tokens, counting.calls, summary=summary,
                   cleared=ctx.cleared_results, summaries=ctx.compactions)


def spilled_text(msgs: list[dict[str, Any]]) -> str:
    """Everything L1 moved out of the context into files the placeholders name."""
    rendered = "\n".join(_render_message(m) for m in msgs)
    paths = re.findall(r"full output: (.+?)\]", rendered)
    return "\n".join(Path(p).read_text(encoding="utf-8") for p in paths if Path(p).is_file())


# ---------- reading back ----------

_READER_PROMPT = """\
Below is the working context of a coding agent, as the agent currently sees it.

<context>
{context}
</context>

Answer each question using ONLY that context. If the context does not contain
the answer, write "unknown" -- do not guess. Reply with one JSON object that
maps each question id to a short answer string, and nothing else.

{questions}"""


def ask(client: LLMClient, model: str, msgs: list[dict[str, Any]], facts: list[Fact]) -> dict[str, str]:
    prompt = _READER_PROMPT.format(
        context="\n\n".join(_render_message(m) for m in msgs),
        questions="\n".join(f"{f.key}: {f.question}" for f in facts))
    response = client.chat(messages=[{"role": "user", "content": prompt}], system="",
                           tools=[], model=model, max_tokens=2_000)
    text = "\n".join(response.text_blocks)
    match = re.search(r"\{.*\}", text, re.S)
    try:
        return {str(k): str(v) for k, v in json.loads(match.group(0)).items()} if match else {}
    except json.JSONDecodeError:
        return {}


def retained(fact: Fact, answer: str) -> bool:
    return fact.answer.lower() in (answer or "").lower()


# ---------- main ----------

async def main_async(args: argparse.Namespace) -> int:
    console = Console()
    load_env_files()
    base = _build_config(argparse.Namespace(profile=args.profile, provider=None, model=None,
                                            base_url=None, api_key=None, mode="auto",
                                            max_turns=None, no_stream=True), load_settings())
    if args.compact_model:
        base.compact_model = args.compact_model
    reader_model = args.reader_model or base.compact_model
    client = build_client(base)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    rows: list[dict[str, Any]] = []
    families = ("tools", "chat")
    for i in range(args.scenarios):
        sc = build_scenario(args.seed + i, families[i % 2])
        before = sum(approx_tokens(_render_message(m)) for m in sc.messages)
        # A window the session overflows by ~10%: L2's line is crossed, so every
        # compacting strategy has to act, and layered decides for itself how far.
        cfg = copy.copy(base)
        cfg.context_window = int(before / 0.75 / 1.1)
        for name in strategies:
            out = await run_strategy(name, sc, client, cfg)
            after = sum(approx_tokens(_render_message(m)) for m in out.messages)
            answers = await asyncio.to_thread(ask, client, reader_model, out.messages, sc.facts)
            hits = {f.key: retained(f, answers.get(f.key, "")) for f in sc.facts}
            in_files = spilled_text(out.messages)
            recoverable = {f.key: not hits[f.key] and f.answer in in_files for f in sc.facts}
            rows.append({"scenario": sc.name, "family": sc.family, "strategy": name,
                         "tokens_before": before, "tokens_after": after,
                         "summarizer_calls": out.summarizer_calls,
                         "summarizer_input_tokens": out.summarizer_input_tokens,
                         "cleared": out.cleared, "summaries": out.summaries,
                         "hits": hits, "recoverable": recoverable, "answers": answers,
                         "summary": out.summary,
                         "expected": {f.key: f.answer for f in sc.facts},
                         "kinds": {f.key: f.kind for f in sc.facts}})
            console.print(f"{sc.name:10} {name:10} {sum(hits.values()):2}/{len(hits)} retained, "
                          f"{before:,} -> {after:,} tokens")

    _print_summary(console, rows, strategies)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"compaction-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"compact_model": base.compact_model, "reader_model": reader_model,
                                "seed": args.seed, "rows": rows}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    console.print(f"\nreport: {path}")
    return 0


def _print_summary(console: Console, rows: list[dict[str, Any]], strategies: list[str]) -> None:
    table = Table(title="Retention after compaction (facts answered correctly)")
    table.add_column("strategy")
    for kind in KINDS:
        table.add_column(kind, justify="right")
    table.add_column("all", justify="right")
    table.add_column("+ in spill file", justify="right")
    table.add_column("tokens kept", justify="right")
    table.add_column("summarizer in-tokens", justify="right")
    for name in strategies:
        rs = [r for r in rows if r["strategy"] == name]
        cells = []
        for kind in (*KINDS, None):
            got = [ok for r in rs for k, ok in r["hits"].items() if kind is None or r["kinds"][k] == kind]
            cells.append(f"{sum(got)}/{len(got)}" if got else "-")
        kept = sum(r["tokens_after"] for r in rs) / max(1, sum(r["tokens_before"] for r in rs))
        hit = sum(sum(r["hits"].values()) for r in rs)
        rec = sum(sum(r["recoverable"].values()) for r in rs)
        n = sum(len(r["hits"]) for r in rs)
        table.add_row(name, *cells, f"{hit + rec}/{n}", f"{kept:.0%}",
                      f"{sum(r['summarizer_input_tokens'] for r in rs):,}")
    console.print(table)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--profile", default=None)
    p.add_argument("--compact-model", default=None, help="Summarizer (default: the profile's).")
    p.add_argument("--reader-model", default=None, help="Model that answers (default: summarizer).")
    p.add_argument("--scenarios", type=int, default=4, help="Alternates tools / chat families.")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--strategies", default=",".join(STRATEGIES))
    return asyncio.run(main_async(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
