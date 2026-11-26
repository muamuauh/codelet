"""Built-in `cockpit` plugin -- real-time observability & ZenMux PAYG balance.

Ported from Agy Cockpit and tailored natively to codelet:
  - Context window gauge + 5-segment breakdown (system / tools / assistant / user / free)
  - Prompt cache hit rate & cumulative token counters
  - Per-turn latency (avg / P95 / TTFT) & throughput (tok/s) + 24-turn trend
  - Cross-session spend/token aggregation (~/.codelet/cockpit_usage.jsonl)
  - Live ZenMux PAYG credit balance via `GET {base_url}/management/payg/balance`
    (requires `ZENMUX_MANAGEMENT_API_KEY` or `LLM_MANAGEMENT_API_KEY`; gracefully
    falls back to local pay-as-you-go spend dashboard when absent)
  - Exposes read-only tool `cockpit_status` and `/cockpit` slash command
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx

from ...llm.models import normalize_base_url
from ...tools.base import Tool, ToolResult
from ..base import PluginContext

DEFAULT_BALANCE_TTL = 300  # 5 minutes
ZENMUX_DEFAULT_BASE = "https://zenmux.ai/api/v1"
RETENTION_DAYS = 30


def default_usage_path() -> Path:
    return Path.home() / ".codelet" / "cockpit_usage.jsonl"


def _resolve_mgmt_key(config: dict[str, Any] | None = None) -> str:
    if isinstance(config, dict) and config.get("management_api_key"):
        return str(config["management_api_key"]).strip()
    return (
        os.environ.get("ZENMUX_MANAGEMENT_API_KEY", "").strip()
        or os.environ.get("LLM_MANAGEMENT_API_KEY", "").strip()
    )


def balance_endpoint(base_url: str | None) -> str:
    norm = normalize_base_url(base_url, provider="openai")
    if norm:
        return f"{norm}/management/payg/balance"
    return f"{ZENMUX_DEFAULT_BASE}/management/payg/balance"


class CockpitStore:
    """Append-only cross-session spend & token log (~/.codelet/cockpit_usage.jsonl)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_usage_path()

    def record(self, entry: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            row = {"ts": time.time(), **entry}
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            if self.path.stat().st_size > 1024 * 1024:
                self._prune()
        except OSError:
            pass

    def _prune(self) -> None:
        cutoff = time.time() - RETENTION_DAYS * 86400
        kept: list[str] = []
        try:
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    if float(obj.get("ts", 0)) >= cutoff:
                        kept.append(line)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
            self.path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        except OSError:
            pass

    def read_rows(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            with self.path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            rows.append(obj)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return rows

    def summary(self, now: float | None = None) -> dict[str, Any]:
        now_ts = now if now is not None else time.time()
        now_dt = datetime.fromtimestamp(now_ts)
        today_str = now_dt.strftime("%Y-%m-%d")

        # Build the 7-day calendar buckets (oldest -> today).
        day_keys: list[str] = []
        daily_map: dict[str, dict[str, Any]] = {}
        for offset in range(6, -1, -1):
            d = now_dt - timedelta(days=offset)
            k = d.strftime("%Y-%m-%d")
            day_keys.append(k)
            daily_map[k] = {
                "date": d.strftime("%m-%d"),
                "is_today": k == today_str,
                "cost_usd": 0.0,
                "tokens": 0,
                "calls": 0,
            }

        cutoff_7d = (now_dt - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today_bucket = {"cost_usd": 0.0, "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0, "calls": 0}
        week_bucket = {"cost_usd": 0.0, "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0, "calls": 0}
        all_bucket = {"cost_usd": 0.0, "tokens": 0, "calls": 0}
        by_model: dict[str, dict[str, Any]] = {}

        for row in self.read_rows():
            try:
                ts = float(row.get("ts", 0))
            except (TypeError, ValueError):
                continue
            ipt = int(row.get("input_tokens", 0) or 0)
            out = int(row.get("output_tokens", 0) or 0)
            cr = int(row.get("cache_read_tokens", 0) or 0)
            cost = float(row.get("cost_usd") or 0.0)
            toks = ipt + out

            all_bucket["cost_usd"] += cost
            all_bucket["tokens"] += toks
            all_bucket["calls"] += 1

            if ts < cutoff_7d:
                continue

            dkey = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            if dkey in daily_map:
                daily_map[dkey]["cost_usd"] += cost
                daily_map[dkey]["tokens"] += toks
                daily_map[dkey]["calls"] += 1

            week_bucket["cost_usd"] += cost
            week_bucket["input_tokens"] += ipt
            week_bucket["cache_read_tokens"] += cr
            week_bucket["output_tokens"] += out
            week_bucket["calls"] += 1

            if dkey == today_str:
                today_bucket["cost_usd"] += cost
                today_bucket["input_tokens"] += ipt
                today_bucket["cache_read_tokens"] += cr
                today_bucket["output_tokens"] += out
                today_bucket["calls"] += 1

            model = str(row.get("model") or "unknown")
            m = by_model.setdefault(model, {"model": model, "cost_usd": 0.0, "tokens": 0, "calls": 0})
            m["cost_usd"] += cost
            m["tokens"] += toks
            m["calls"] += 1

        for b in (today_bucket, week_bucket, all_bucket):
            b["cost_usd"] = round(b["cost_usd"], 6)
        for d in daily_map.values():
            d["cost_usd"] = round(d["cost_usd"], 6)
        models_7d = sorted(
            by_model.values(),
            key=lambda x: (x["cost_usd"], x["tokens"]),
            reverse=True,
        )[:6]
        for m in models_7d:
            m["cost_usd"] = round(m["cost_usd"], 6)

        return {
            "today": today_bucket,
            "last_7d": week_bucket,
            "all_time": all_bucket,
            "daily_7d": [daily_map[k] for k in day_keys],
            "models_7d": models_7d,
        }


def parse_zenmux_balance(payload: Any) -> dict[str, Any]:
    """Parse ZenMux `GET /api/v1/management/payg/balance` response."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid balance payload")
    if payload.get("success") is False:
        raise RuntimeError(str(payload.get("message") or payload.get("error") or "balance query failed"))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if "total_credits" not in data:
        raise ValueError("Response missing total_credits")
    return {
        "currency": str(data.get("currency") or "USD").upper(),
        "total_credits": round(float(data["total_credits"]), 4),
        "top_up_credits": round(float(data.get("top_up_credits", 0.0) or 0.0), 4),
        "bonus_credits": round(float(data.get("bonus_credits", 0.0) or 0.0), 4),
    }


def fetch_zenmux_balance(base_url: str | None, mgmt_key: str, *, timeout: float = 10.0) -> dict[str, Any]:
    url = balance_endpoint(base_url)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers={"Authorization": f"Bearer {mgmt_key}", "Accept": "application/json"})
        resp.raise_for_status()
        return parse_zenmux_balance(resp.json())


async def afetch_zenmux_balance(base_url: str | None, mgmt_key: str, *, timeout: float = 10.0) -> dict[str, Any]:
    url = balance_endpoint(base_url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {mgmt_key}", "Accept": "application/json"})
        resp.raise_for_status()
        return parse_zenmux_balance(resp.json())


class ZenMuxBalanceClient:
    """TTL-cached reader for ZenMux PAYG credit balance."""

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        ttl_seconds: int = DEFAULT_BALANCE_TTL,
        fetcher: Callable[..., dict[str, Any]] | None = None,
        afetcher: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config or {}
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._fetcher = fetcher or fetch_zenmux_balance
        self._afetcher = afetcher or afetch_zenmux_balance
        self._clock = clock or time.time
        self._cached: dict[str, Any] | None = None
        self._fetched_at: float | None = None

    def cached_view(self) -> dict[str, Any]:
        key = _resolve_mgmt_key(self._config)
        if not key:
            return {
                "configured": False,
                "available": False,
                "currency": "USD",
                "total_credits": None,
                "top_up_credits": None,
                "bonus_credits": None,
                "fetched_at": None,
                "error": None,
            }
        if self._cached is not None:
            return {
                "configured": True,
                "available": True,
                **self._cached,
                "fetched_at": self._fetched_at,
                "error": None,
            }
        return {
            "configured": True,
            "available": False,
            "currency": "USD",
            "total_credits": None,
            "top_up_credits": None,
            "bonus_credits": None,
            "fetched_at": None,
            "error": None,
        }

    def _is_fresh(self) -> bool:
        if self._cached is None or self._fetched_at is None:
            return False
        return (self._clock() - self._fetched_at) < self.ttl_seconds

    def get(self, base_url: str | None, *, force: bool = False) -> dict[str, Any]:
        key = _resolve_mgmt_key(self._config)
        if not key:
            return self.cached_view()
        if not force and self._is_fresh():
            return self.cached_view()
        try:
            data = self._fetcher(base_url, key)
            self._cached = data
            self._fetched_at = self._clock()
            return self.cached_view()
        except Exception as exc:
            view = self.cached_view()
            view["error"] = f"{type(exc).__name__}: {exc}"
            return view

    async def aget(self, base_url: str | None, *, force: bool = False) -> dict[str, Any]:
        key = _resolve_mgmt_key(self._config)
        if not key:
            return self.cached_view()
        if not force and self._is_fresh():
            return self.cached_view()
        try:
            data = await self._afetcher(base_url, key)
            self._cached = data
            self._fetched_at = self._clock()
            return self.cached_view()
        except Exception as exc:
            view = self.cached_view()
            view["error"] = f"{type(exc).__name__}: {exc}"
            return view


class CockpitRuntime:
    """Attached to `AgentLoop._cockpit` when the cockpit plugin is active."""

    def __init__(self, host: Any, store: CockpitStore, balance: ZenMuxBalanceClient) -> None:
        self.host = host
        self.store = store
        self.balance = balance

    def snapshot(self, *, balance_view: dict[str, Any] | None = None) -> dict[str, Any]:
        agent = self.host
        schemas = [
            s for s in agent.registry.api_schemas()
            if s.get("name") not in agent._disabled_tools
        ]
        ctx = agent.context.breakdown(tool_schemas=schemas)
        tool_counts: dict[str, int] = getattr(agent, "_tool_call_counts", {}) or {}
        top_tools = [
            {"name": k, "count": v}
            for k, v in sorted(tool_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
        ]
        ctx["top_tools"] = top_tools
        ctx["total_tool_calls"] = sum(tool_counts.values())

        usage = self.store.summary()
        bal = dict(balance_view if balance_view is not None else self.balance.cached_view())
        est_days_left: float | None = None
        if bal.get("available") and bal.get("total_credits") is not None:
            daily_avg = (usage["last_7d"]["cost_usd"] / 7.0) if usage["last_7d"]["cost_usd"] > 0 else 0.0
            if daily_avg > 0:
                est_days_left = round(float(bal["total_credits"]) / daily_avg, 1)
        bal["est_days_left"] = est_days_left

        return {
            "enabled": True,
            "model": agent.config.model,
            "provider": agent.config.provider.value,
            "mode": agent.config.permission_mode.value,
            "reasoning_effort": getattr(agent.config, "reasoning_effort", "auto"),
            "context": ctx,
            "cache": agent.telemetry.cache_stats(),
            "perf": agent.telemetry.perf_summary(),
            "session": agent.telemetry.snapshot(),
            "usage": usage,
            "balance": bal,
            "updated_at": time.time(),
        }


class CockpitStatusTool(Tool):
    read_only = True

    def __init__(self, runtime: CockpitRuntime) -> None:
        self._rt = runtime

    @property
    def name(self) -> str:
        return "cockpit_status"

    @property
    def description(self) -> str:
        return (
            "Inspect real-time session cockpit telemetry: context window usage %, "
            "cache hit rate, latency/throughput speed, execution mode, and spend/balance."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        snap = self._rt.snapshot()
        ctx = snap["context"]
        cache = snap["cache"]
        perf = snap["perf"]
        bal = snap["balance"]
        lines = [
            f"Model: {snap['model']} | Mode: {snap['mode']}",
            f"Context: {ctx['used']:,} / {ctx['limit']:,} ({ctx['pct']}%) | Compactions: {ctx['compactions']}",
            f"Cache hit: {cache['cumulative_hit_pct']}% (latest {cache['latest_hit_pct']}%)",
            f"Speed: {perf['latest_speed_tps'] or 'n/a'} tok/s | Latency: {perf['latest_latency_ms'] or 'n/a'} ms",
            f"Spend: today ${snap['usage']['today']['cost_usd']:.4f} | 7d ${snap['usage']['last_7d']['cost_usd']:.4f}",
        ]
        if bal.get("available"):
            lines.append(
                f"ZenMux Balance: ${bal['total_credits']:.2f} "
                f"(top-up ${bal['top_up_credits']:.2f}, bonus ${bal['bonus_credits']:.2f})"
            )
        return ToolResult(output="\n".join(lines))


class CockpitPlugin:
    name = "cockpit"
    description = "Real-time context gauge, cache hit rate, speed telemetry & ZenMux PAYG balance."

    def setup(self, ctx: PluginContext) -> None:
        usage_path = ctx.config.get("usage_path")
        store = CockpitStore(path=usage_path)
        ttl = int(ctx.config.get("balance_ttl_seconds", DEFAULT_BALANCE_TTL))
        balance = ZenMuxBalanceClient(config=ctx.config, ttl_seconds=ttl)

        if ctx.host is None:
            return

        runtime = CockpitRuntime(ctx.host, store, balance)
        ctx.host._cockpit = runtime

        def _on_record(row: Any) -> None:
            store.record({
                "project": os.path.basename(os.getcwd()) or os.getcwd(),
                "model": row.model or ctx.host.config.model,
                "input_tokens": row.input_tokens,
                "cache_read_tokens": row.cache_read_tokens,
                "output_tokens": row.output_tokens,
                "cost_usd": row.cost_usd,
                "latency_ms": row.latency_ms,
            })

        ctx.host.telemetry.on_record.append(_on_record)
        ctx.register_tool(CockpitStatusTool(runtime))
        ctx.register_skill(
            name="session-diagnostics",
            description="Diagnose context window growth, prompt cache hit rate, latency, and PAYG spend",
            category="observability",
            triggers=("context", "slow", "cost", "cache", "cockpit"),
            allowed_tools=("cockpit_status",),
            body=(
                "# Session Diagnostics Procedure\n\n"
                "1. Call `cockpit_status` first to inspect live context utilization, cache hit rate, "
                "throughput (tok/s), and spend.\n"
                "2. If context utilization exceeds 70%, recommend `/compact <focus>` or starting a fresh thread.\n"
                "3. If cache hit rate is low (<40%), point out what is invalidating the prefix (e.g. frequent mode/tool churn).\n"
                "4. Present findings concisely as actionable bullets."
            ),
        )

        def _cmd_cockpit(args: str) -> str:
            bal = balance.get(ctx.host.config.base_url, force=("refresh" in args.lower()))
            snap = runtime.snapshot(balance_view=bal)
            c = snap["context"]
            p = snap["perf"]
            b = snap["balance"]
            bal_str = (
                f" | balance ${b['total_credits']:.2f}"
                if b.get("available")
                else f" | today ${snap['usage']['today']['cost_usd']:.4f}"
            )
            return (
                f"cockpit · {snap['model']} [{snap['mode']}] · "
                f"ctx {c['pct']}% ({c['used']:,}/{c['limit']:,}) · "
                f"cache {snap['cache']['cumulative_hit_pct']}% · "
                f"speed {p['latest_speed_tps'] or '—'} tok/s{bal_str}"
            )

        ctx.register_command("cockpit", _cmd_cockpit)


PLUGIN = CockpitPlugin()
