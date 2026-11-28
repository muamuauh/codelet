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

from dataclasses import asdict, dataclass, field
import time
from typing import Any, Callable

from rich.panel import Panel
from rich.table import Table

DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00},
    "claude-opus-4-7":   {"input": 15.0, "output": 75.00},
    # Older snapshots:
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
}


def resolve_input_tokens(usage: dict[str, Any] | None) -> tuple[int, int, int, int]:
    """Normalize provider-specific input/cache token usage.

    Returns ``(total_input, uncached_input, cache_read, cache_write)``.

    Two reporting conventions exist in the wild:
    - **Anthropic / Claude-passthrough relays (exclusive)**: ``input_tokens``
      counts only *uncached* new tokens; ``cache_read_tokens`` and
      ``cache_write_tokens`` are reported separately and must be added to get
      the true prompt/context size.
    - **OpenAI-compatible (inclusive)**: ``prompt_tokens`` (mapped to
      ``input_tokens``) already includes ``cached_tokens``.

    When ``cache_exclusive`` is explicitly set (by ``AnthropicClient`` or
    detected by ``OpenAICompatClient``), or when ``input_tokens < cache_read +
    cache_write`` (mathematically impossible under inclusive accounting), we
    treat the payload as exclusive.
    """
    if not usage:
        return 0, 0, 0, 0
    ipt = max(0, int(usage.get("input_tokens", 0) or 0))
    cr = max(0, int(usage.get("cache_read_tokens", 0) or 0))
    cw = max(0, int(usage.get("cache_write_tokens", 0) or 0))
    explicit_total = usage.get("total_input_tokens")

    exclusive = bool(usage.get("cache_exclusive")) or ((cr + cw) > 0 and ipt < (cr + cw))
    if exclusive:
        total_in = int(explicit_total) if explicit_total is not None else (ipt + cr + cw)
        total_in = max(total_in, ipt + cr + cw)
        uncached_in = ipt
    else:
        total_in = int(explicit_total) if explicit_total is not None else ipt
        uncached_in = max(0, total_in - cr - cw)
    return total_in, uncached_in, cr, cw


@dataclass
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None  # None if model not priced
    model: str = ""
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    uncached_input_tokens: int | None = None
    latency_ms: float = 0.0
    ttft_ms: float | None = None
    context_tokens: int = 0
    ts: float = 0.0

    @property
    def effective_uncached_input(self) -> int:
        if self.uncached_input_tokens is not None:
            return self.uncached_input_tokens
        return max(0, self.input_tokens - self.cache_read_tokens - self.cache_write_tokens)

    @property
    def speed_tps(self) -> float | None:
        if self.output_tokens > 0 and self.latency_ms > 50:
            return round(self.output_tokens / (self.latency_ms / 1000.0), 1)
        return None


@dataclass
class Telemetry:
    pricing: dict[str, dict[str, float]] = field(default_factory=lambda: dict(DEFAULT_PRICING))
    turns: list[TurnUsage] = field(default_factory=list)
    last_turn_start_index: int = 0  # marker for "this user turn started here"
    legacy_call_offset: int = 0     # extra call count preserved from legacy summary-only snapshots
    on_record: list[Callable[[TurnUsage], None]] = field(default_factory=list)

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

    def record_chat(
        self,
        model: str,
        usage: dict[str, Any],
        *,
        context_tokens: int = 0,
    ) -> TurnUsage:
        """Append one LLM-call usage record. Returns the row for callers that
        want to log it inline."""
        total_in, uncached_in, cache_read, cache_write = resolve_input_tokens(usage)
        out = int(usage.get("output_tokens", 0) or 0)
        latency_ms = float(usage.get("latency_ms", 0.0) or 0.0)
        ttft_raw = usage.get("ttft_ms")
        ttft_ms = float(ttft_raw) if ttft_raw is not None else None
        cost: float | None
        prices = self.pricing.get(model)
        if prices:
            in_price = prices["input"]
            out_price = prices["output"]
            cr_price = prices.get("cache_read", in_price * 0.1)
            cw_price = prices.get("cache_write", in_price * 1.25)
            cost = (
                (uncached_in / 1_000_000) * in_price
                + (cache_read / 1_000_000) * cr_price
                + (cache_write / 1_000_000) * cw_price
                + (out / 1_000_000) * out_price
            )
        else:
            cost = None
        row = TurnUsage(
            input_tokens=total_in,
            output_tokens=out,
            cost_usd=cost,
            model=model,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            uncached_input_tokens=uncached_in,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            context_tokens=context_tokens or total_in,
            ts=time.time(),
        )
        self.turns.append(row)
        for cb in list(self.on_record):
            try:
                cb(row)
            except Exception:
                pass
        return row

    def cache_stats(self) -> dict[str, Any]:
        """Latest and cumulative prompt cache hit rate."""
        def _rate(total_in: int, cr: int) -> float:
            if cr <= 0 or total_in <= 0:
                return 0.0
            return round(min(1.0, cr / total_in) * 100.0, 1)

        tot_in = sum(t.input_tokens for t in self.turns)
        tot_uncached = sum(t.effective_uncached_input for t in self.turns)
        tot_cache = sum(t.cache_read_tokens for t in self.turns)
        tot_out = sum(t.output_tokens for t in self.turns)
        latest = self.turns[-1] if self.turns else None
        latest_in = latest.input_tokens if latest else 0
        latest_cache = latest.cache_read_tokens if latest else 0
        latest_uncached = latest.effective_uncached_input if latest else 0
        return {
            "latest_hit_pct": _rate(latest_in, latest_cache),
            "cumulative_hit_pct": _rate(tot_in, tot_cache),
            "latest_uncached_in": latest_uncached,
            "latest_cache_read": latest_cache,
            "latest_out": latest.output_tokens if latest else 0,
            "total_uncached_in": tot_uncached,
            "total_cache_read": tot_cache,
            "total_out": tot_out,
            "total_calls": len(self.turns) + self.legacy_call_offset,
        }

    def perf_summary(self, limit: int = 24) -> dict[str, Any]:
        """Latency (ms/s) and output throughput (tok/s) summary + recent trend."""
        with_lat = [t for t in self.turns if t.latency_ms > 0]
        speeds = [s for t in with_lat if (s := t.speed_tps) is not None]
        lats = sorted(t.latency_ms for t in with_lat)

        def _p95(vals: list[float]) -> float | None:
            if not vals:
                return None
            idx = min(len(vals) - 1, max(0, int(len(vals) * 0.95) - (1 if len(vals) > 1 else 0)))
            return round(vals[idx], 1)

        latest = with_lat[-1] if with_lat else None
        recent = self.turns[-limit:]
        series = [
            {
                "idx": idx + 1,
                "speed": t.speed_tps or 0.0,
                "latency_s": round(t.latency_ms / 1000.0, 2),
                "context_k": round((t.context_tokens or t.input_tokens) / 1000.0, 1),
                "out": t.output_tokens,
            }
            for idx, t in enumerate(recent)
        ]
        return {
            "latest_latency_ms": round(latest.latency_ms, 1) if latest else None,
            "latest_ttft_ms": round(latest.ttft_ms, 1) if (latest and latest.ttft_ms is not None) else None,
            "latest_speed_tps": latest.speed_tps if latest else None,
            "avg_latency_ms": round(sum(lats) / len(lats), 1) if lats else None,
            "p95_latency_ms": _p95(lats),
            "avg_speed_tps": round(sum(speeds) / len(speeds), 1) if speeds else None,
            "peak_speed_tps": round(max(speeds), 1) if speeds else None,
            "samples": len(with_lat),
            "series": series,
        }

    def update_pricing(self, overrides: dict[str, dict[str, float]] | None) -> None:
        if not overrides:
            return
        for model, table in overrides.items():
            if isinstance(table, dict) and "input" in table and "output" in table:
                self.pricing[model] = {
                    "input": float(table["input"]),
                    "output": float(table["output"]),
                }

    def to_dict(self) -> dict[str, Any]:
        """Serialize full per-call telemetry + legacy summary fields for session snapshots."""
        cum = self.cumulative
        out: dict[str, Any] = {
            "input_tokens": cum.input_tokens,
            "output_tokens": cum.output_tokens,
            "cost_usd": cum.cost_usd,
            "calls": len(self.turns) + self.legacy_call_offset,
            "last_turn_start_index": self.last_turn_start_index,
            "turns": [asdict(t) for t in self.turns],
        }
        if self.legacy_call_offset > 0:
            out["legacy_call_offset"] = self.legacy_call_offset
        return out

    def load_dict(self, data: dict[str, Any] | None) -> None:
        """Restore telemetry from a session snapshot (supports both full `turns`
        snapshots and legacy summary-only snapshots)."""
        self.turns = []
        self.last_turn_start_index = 0
        self.legacy_call_offset = 0
        if not isinstance(data, dict):
            return
        raw_turns = data.get("turns")
        if isinstance(raw_turns, list) and raw_turns:
            for item in raw_turns:
                if not isinstance(item, dict):
                    continue
                try:
                    cost_raw = item.get("cost_usd")
                    ttft_raw = item.get("ttft_ms")
                    uncached_raw = item.get("uncached_input_tokens")
                    self.turns.append(TurnUsage(
                        input_tokens=int(item.get("input_tokens") or 0),
                        output_tokens=int(item.get("output_tokens") or 0),
                        cost_usd=float(cost_raw) if cost_raw is not None else None,
                        model=str(item.get("model") or ""),
                        cache_read_tokens=int(item.get("cache_read_tokens") or 0),
                        cache_write_tokens=int(item.get("cache_write_tokens") or 0),
                        uncached_input_tokens=int(uncached_raw) if uncached_raw is not None else None,
                        latency_ms=float(item.get("latency_ms") or 0.0),
                        ttft_ms=float(ttft_raw) if ttft_raw is not None else None,
                        context_tokens=int(item.get("context_tokens") or 0),
                        ts=float(item.get("ts") or 0.0),
                    ))
                except (TypeError, ValueError):
                    continue
            idx = int(data.get("last_turn_start_index") or 0)
            self.last_turn_start_index = max(0, min(len(self.turns), idx))
            self.legacy_call_offset = max(0, int(data.get("legacy_call_offset") or 0))
            return

        # Backward compatibility: older snapshots only stored aggregate totals.
        ipt = int(data.get("input_tokens") or 0)
        out = int(data.get("output_tokens") or 0)
        legacy_calls = max(0, int(data.get("calls") or 0))
        cost_raw = data.get("cost_usd")
        if ipt > 0 or out > 0:
            self.turns.append(TurnUsage(
                input_tokens=ipt,
                output_tokens=out,
                cost_usd=float(cost_raw) if cost_raw is not None else None,
                uncached_input_tokens=ipt,
            ))
            self.last_turn_start_index = 0
            self.legacy_call_offset = max(0, legacy_calls - 1)
        elif legacy_calls > 0:
            self.legacy_call_offset = legacy_calls

    def snapshot(self) -> dict[str, Any]:
        """Plain-data view of this-turn + cumulative usage, for non-Rich UIs
        (the web GUI). Mirrors what `render_panel` shows, as numbers."""
        per_turn_rows = self.turns[self.last_turn_start_index:]
        turn = TurnUsage(0, 0, 0.0)
        any_priced = False
        for r in per_turn_rows:
            turn.input_tokens += r.input_tokens
            turn.output_tokens += r.output_tokens
            if r.cost_usd is not None:
                any_priced = True
                turn.cost_usd = (turn.cost_usd or 0.0) + r.cost_usd
        if not any_priced:
            turn.cost_usd = None
        cum = self.cumulative
        return {
            "turn": {"input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens,
                     "cost_usd": turn.cost_usd},
            "session": {"input_tokens": cum.input_tokens, "output_tokens": cum.output_tokens,
                        "cost_usd": cum.cost_usd},
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
