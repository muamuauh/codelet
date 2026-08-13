"""Token + cost telemetry.

Maintained as a lightweight per-AgentLoop accumulator. The agent_loop calls
`record_chat()` after each LLM call. The CLI calls `render_panel()` at the
end of each user turn for the human view; the model never sees telemetry.

Pricing is in USD per million tokens. Defaults are conservative and easy to
override via settings.json:

    {"pricing": {"claude-sonnet-4-5": {"input": 3.0, "output": 15.0}}}

If a model isn't in the pricing table we still count tokens but report cost
as "n/a". Cache hits / writes are not modeled in P4 (Claude API exposes them
in `usage.cache_read_input_tokens` etc.; we'll wire them up if the model
starts heavily caching).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rich.panel import Panel
from rich.table import Table

DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00},
    "claude-opus-4-7":   {"input": 15.0, "output": 75.00},
    # Older snapshots:
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
}


@dataclass
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None  # None if model not priced


@dataclass
class Telemetry:
    pricing: dict[str, dict[str, float]] = field(default_factory=lambda: dict(DEFAULT_PRICING))
    turns: list[TurnUsage] = field(default_factory=list)
    last_turn_start_index: int = 0  # marker for "this user turn started here"

    @property
    def cumulative(self) -> TurnUsage:
        total = TurnUsage(0, 0, 0.0)
        any_priced = False
        for t in self.turns:
            total.input_tokens += t.input_tokens
            total.output_tokens += t.output_tokens
            if t.cost_usd is not None:
                any_priced = True
                total.cost_usd = (total.cost_usd or 0.0) + t.cost_usd
        if not any_priced:
            total.cost_usd = None
        return total

    def begin_user_turn(self) -> None:
        """Mark where the next user-turn block starts in `self.turns`.

        Used by `render_panel` to break out per-turn vs cumulative counts.
        """
        self.last_turn_start_index = len(self.turns)

    def record_chat(self, model: str, usage: dict[str, int]) -> TurnUsage:
        """Append one LLM-call usage record. Returns the row for callers that
        want to log it inline."""
        ipt = int(usage.get("input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        cost: float | None
        prices = self.pricing.get(model)
        if prices:
            cost = (ipt / 1_000_000) * prices["input"] + (out / 1_000_000) * prices["output"]
        else:
            cost = None
        row = TurnUsage(input_tokens=ipt, output_tokens=out, cost_usd=cost)
        self.turns.append(row)
        return row

    def update_pricing(self, overrides: dict[str, dict[str, float]] | None) -> None:
        if not overrides:
            return
        for model, table in overrides.items():
            if isinstance(table, dict) and "input" in table and "output" in table:
                self.pricing[model] = {
                    "input": float(table["input"]),
                    "output": float(table["output"]),
                }

    def render_panel(self) -> Panel:
        """Build a Rich panel summarizing this user turn + cumulative session.

        Per-turn = sum of TurnUsage rows since the last `begin_user_turn`.
        """
        per_turn_rows = self.turns[self.last_turn_start_index:]
        per_turn = TurnUsage(0, 0, 0.0)
        any_priced = False
        for r in per_turn_rows:
            per_turn.input_tokens += r.input_tokens
            per_turn.output_tokens += r.output_tokens
            if r.cost_usd is not None:
                any_priced = True
                per_turn.cost_usd = (per_turn.cost_usd or 0.0) + r.cost_usd
        if not any_priced:
            per_turn.cost_usd = None

        cum = self.cumulative
        table = Table(show_header=True, header_style="bold dim")
        table.add_column("", width=10)
        table.add_column("input", justify="right")
        table.add_column("output", justify="right")
        table.add_column("USD", justify="right")
        table.add_row(
            "this turn",
            f"{per_turn.input_tokens:,}",
            f"{per_turn.output_tokens:,}",
            _fmt_cost(per_turn.cost_usd),
        )
        table.add_row(
            "session",
            f"{cum.input_tokens:,}",
            f"{cum.output_tokens:,}",
            _fmt_cost(cum.cost_usd),
        )
        return Panel(table, title="usage", border_style="dim", expand=False)


def _fmt_cost(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.4f}"
