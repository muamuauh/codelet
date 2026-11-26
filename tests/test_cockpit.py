"""Tests for the built-in Cockpit plugin, 4-mode permissions, /btw isolation,
context breakdown/compaction, telemetry cache/perf stats, and web endpoints.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from codelet.agent_loop import AgentLoop
from codelet.config import Config, LLMProvider, PermissionMode
from codelet.context import ConversationContext
from codelet.events import NullSink
from codelet.llm.base import LLMClient, LLMResponse, ToolCall
from codelet.llm.openai_compat import OpenAICompatClient
from codelet.permissions import PermissionGate
from codelet.plugins.builtin.cockpit import (
    CockpitStore,
    ZenMuxBalanceClient,
    balance_endpoint,
    parse_zenmux_balance,
)
from codelet.telemetry import Telemetry, TurnUsage
from codelet.tools.bash_tool import BashTool
from codelet.tools.file_read import FileReadTool
from codelet.tools.file_write import FileWriteTool


# ---------------------------------------------------------------------------
# CockpitStore: cross-session JSONL aggregation
# ---------------------------------------------------------------------------


def test_cockpit_store_record_and_summary(tmp_path: Path):
    store = CockpitStore(path=tmp_path / "usage.jsonl")
    base_dt = datetime(2026, 8, 15, 14, 0, 0)
    today_ts = base_dt.timestamp()
    three_days_ago_ts = (base_dt - timedelta(days=3)).timestamp()
    ten_days_ago_ts = (base_dt - timedelta(days=10)).timestamp()

    store.record({
        "ts": today_ts,
        "model": "qwen/qwen3.8-max",
        "input_tokens": 1000,
        "cache_read_tokens": 400,
        "output_tokens": 200,
        "cost_usd": 0.05,
    })
    store.record({
        "ts": three_days_ago_ts,
        "model": "moonshotai/kimi-k3",
        "input_tokens": 2000,
        "cache_read_tokens": 0,
        "output_tokens": 500,
        "cost_usd": 0.12,
    })
    store.record({
        "ts": ten_days_ago_ts,
        "model": "old/model",
        "input_tokens": 5000,
        "cache_read_tokens": 0,
        "output_tokens": 1000,
        "cost_usd": 0.50,
    })

    s = store.summary(now=today_ts)
    assert s["today"]["calls"] == 1
    assert s["today"]["cost_usd"] == pytest.approx(0.05)
    assert s["today"]["cache_read_tokens"] == 400

    assert s["last_7d"]["calls"] == 2
    assert s["last_7d"]["cost_usd"] == pytest.approx(0.17)

    assert s["all_time"]["calls"] == 3
    assert s["all_time"]["cost_usd"] == pytest.approx(0.67)

    assert len(s["daily_7d"]) == 7
    assert s["daily_7d"][-1]["is_today"] is True
    assert s["daily_7d"][-1]["cost_usd"] == pytest.approx(0.05)

    models = [m["model"] for m in s["models_7d"]]
    assert models == ["moonshotai/kimi-k3", "qwen/qwen3.8-max"]


# ---------------------------------------------------------------------------
# ZenMux PAYG balance parser & TTL cache
# ---------------------------------------------------------------------------


def test_parse_zenmux_balance_and_endpoint():
    assert balance_endpoint("https://zenmux.ai/api/v1/") == "https://zenmux.ai/api/v1/management/payg/balance"
    assert balance_endpoint(None) == "https://zenmux.ai/api/v1/management/payg/balance"

    parsed = parse_zenmux_balance({
        "success": True,
        "data": {
            "currency": "usd",
            "total_credits": 42.5,
            "top_up_credits": 40.0,
            "bonus_credits": 2.5,
        },
    })
    assert parsed == {
        "currency": "USD",
        "total_credits": 42.5,
        "top_up_credits": 40.0,
        "bonus_credits": 2.5,
    }

    with pytest.raises(RuntimeError):
        parse_zenmux_balance({"success": False, "message": "unauthorized"})


def test_zenmux_balance_client_unconfigured_and_ttl_cache(monkeypatch):
    monkeypatch.delenv("ZENMUX_MANAGEMENT_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MANAGEMENT_API_KEY", raising=False)

    client_unconf = ZenMuxBalanceClient()
    view = client_unconf.get("https://zenmux.ai/api/v1")
    assert view["configured"] is False
    assert view["available"] is False

    now = [1000.0]
    calls = [0]

    def fake_fetch(base_url, key):
        calls[0] += 1
        return {
            "currency": "USD",
            "total_credits": 100.0 - calls[0],
            "top_up_credits": 90.0,
            "bonus_credits": 10.0 - calls[0],
        }

    client = ZenMuxBalanceClient(
        config={"management_api_key": "sk-mgmt-test"},
        ttl_seconds=60,
        fetcher=fake_fetch,
        clock=lambda: now[0],
    )
    v1 = client.get("https://zenmux.ai/api/v1")
    assert v1["available"] is True and v1["total_credits"] == 99.0
    assert calls[0] == 1

    # Within TTL -> cache hit, no extra fetch
    now[0] += 30.0
    v2 = client.get("https://zenmux.ai/api/v1")
    assert v2["total_credits"] == 99.0 and calls[0] == 1

    # Force refresh -> fetches immediately
    v3 = client.get("https://zenmux.ai/api/v1", force=True)
    assert v3["total_credits"] == 98.0 and calls[0] == 2

    # Error falls back gracefully while preserving cached view + error message
    def boom(base_url, key):
        raise RuntimeError("network down")

    client._fetcher = boom
    v4 = client.get("https://zenmux.ai/api/v1", force=True)
    assert v4["available"] is True
    assert v4["total_credits"] == 98.0
    assert "network down" in (v4["error"] or "")


# ---------------------------------------------------------------------------
# Telemetry cache_stats & perf_summary
# ---------------------------------------------------------------------------


def test_telemetry_cache_and_perf_summary():
    t = Telemetry()
    received: list[TurnUsage] = []
    t.on_record.append(received.append)

    t.record_chat("qwen/qwen3.8-max", {
        "input_tokens": 1000,
        "output_tokens": 100,
        "cache_read_tokens": 600,
        "cache_write_tokens": 50,
        "latency_ms": 2000,
        "ttft_ms": 400,
    }, context_tokens=3000)
    t.record_chat("qwen/qwen3.8-max", {
        "input_tokens": 1200,
        "output_tokens": 200,
        "cache_read_tokens": 900,
        "latency_ms": 1000,
        "ttft_ms": 250,
    }, context_tokens=4500)

    assert len(received) == 2
    cache = t.cache_stats()
    assert cache["total_cache_read"] == 1500
    assert cache["total_calls"] == 2
    assert cache["latest_hit_pct"] == 75.0
    assert cache["cumulative_hit_pct"] == pytest.approx(round(1500 / 2200 * 100, 1))

    perf = t.perf_summary()
    assert perf["samples"] == 2
    assert perf["latest_latency_ms"] == 1000
    assert perf["avg_latency_ms"] == 1500
    assert perf["latest_ttft_ms"] == 250
    assert perf["latest_speed_tps"] == pytest.approx(200.0)
    assert perf["peak_speed_tps"] == pytest.approx(200.0)
    assert len(perf["series"]) == 2


# ---------------------------------------------------------------------------
# Context breakdown & manual force_compact
# ---------------------------------------------------------------------------


class _SummaryClient(LLMClient):
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def chat(self, *, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        self.prompts.append(str(messages[0]["content"]))
        return LLMResponse(
            text_blocks=["Concise summary preserving key facts."],
            raw_content=[{"type": "text", "text": "Concise summary preserving key facts."}],
            stop_reason="end_turn",
            usage={"input_tokens": 50, "output_tokens": 20},
        )


def test_context_breakdown_and_force_compact():
    cfg = Config(context_window=10_000)
    ctx = ConversationContext(cfg)
    ctx.set_system_prompt("You are helpful.")
    for i in range(6):
        ctx.add_user_message(f"user message {i} " + "x" * 200)
        ctx.add_assistant_message([{"type": "text", "text": f"assistant reply {i} " + "y" * 200}])

    bd = ctx.breakdown(tool_schemas=[{"name": "read_file", "description": "Read a file"}])
    segs = bd["segments"]
    assert bd["limit"] == 10_000
    assert bd["used"] > 0
    assert segs["system"] > 0
    assert segs["tools"] > 0
    assert segs["user"] > 0
    assert segs["assistant"] > 0
    assert bd["used"] + segs["free"] == max(bd["limit"], bd["used"])

    client = _SummaryClient()
    res = asyncio.run(ctx.force_compact(client, focus="keep auth bugs"))
    assert res["compacted"] is True
    assert res["after_messages"] < res["before_messages"]
    assert any("keep auth bugs" in p for p in client.prompts)


# ---------------------------------------------------------------------------
# 4-mode PermissionGate matrix (plan / ask / normal / auto)
# ---------------------------------------------------------------------------


def test_permission_gate_four_modes():
    read_tool = FileReadTool()
    write_tool = FileWriteTool()
    bash_tool = BashTool()

    # PLAN: read-only only
    g_plan = PermissionGate(Config(permission_mode=PermissionMode.PLAN))
    assert g_plan.check(read_tool, {"path": "a.py"}) is None
    assert g_plan.check(write_tool, {"path": "a.py", "content": "x"}) is not None
    assert g_plan.check(bash_tool, {"command": "git status"}) is None
    assert g_plan.check(bash_tool, {"command": "pytest -q"}) is not None

    # ASK: writes confirm, non-read-only bash (`python -m pytest -q`) STILL needs confirm, read-only bash (`git status`) auto-allowed
    g_ask = PermissionGate(Config(permission_mode=PermissionMode.ASK))
    assert g_ask.needs_bash_confirmation(bash_tool, {"command": "git status"}) is False
    assert g_ask.needs_bash_confirmation(bash_tool, {"command": "python -m pytest -q"}) is True
    assert g_ask.check(bash_tool, {"command": "python -m pytest -q"}, defer_confirm=True) is None

    # NORMAL: writes confirm, allowed dev bash (`python -m pytest -q`) auto-allowed, unknown bash (`curl ...`) needs confirm
    g_normal = PermissionGate(Config(permission_mode=PermissionMode.NORMAL))
    assert g_normal.needs_bash_confirmation(bash_tool, {"command": "python -m pytest -q"}) is False
    assert g_normal.needs_bash_confirmation(bash_tool, {"command": "curl https://example.com"}) is True
    assert g_normal.check(write_tool, {"path": "a.py", "content": "x"}, defer_confirm=True) is None

    # AUTO: auto-allows writes & normal commands, still denies destructive patterns
    g_auto = PermissionGate(Config(permission_mode=PermissionMode.AUTO))
    assert g_auto.needs_bash_confirmation(bash_tool, {"command": "curl https://example.com"}) is False
    assert g_auto.check(write_tool, {"path": "a.py", "content": "x"}) is None
    res = g_auto.check(bash_tool, {"command": "rm -rf /"})
    assert res is not None and res.is_error


# ---------------------------------------------------------------------------
# OpenAICompatClient cache & reasoning parsing
# ---------------------------------------------------------------------------


def test_openai_compat_parses_cache_and_reasoning():
    from types import SimpleNamespace

    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(
                role="assistant",
                reasoning_content="step 1: check types",
                content="final answer",
                tool_calls=None,
            ),
        )],
        usage=SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=50,
            prompt_tokens_details=SimpleNamespace(cached_tokens=750),
        ),
    )
    resp = OpenAICompatClient._to_internal_response(fake_resp)
    assert resp.text_blocks == ["final answer"]
    assert resp.reasoning_blocks == ["step 1: check types"]
    assert resp.usage["input_tokens"] == 1000
    assert resp.usage["output_tokens"] == 50
    assert resp.usage["cache_read_tokens"] == 750


# ---------------------------------------------------------------------------
# /btw side-panel isolation in AgentLoop
# ---------------------------------------------------------------------------


class _BtwStubClient(LLMClient):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(self, *, on_text: Any = None, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        reply = "side answer" if not kwargs.get("tools") else "main answer"
        if on_text is not None:
            on_text(reply)
        return LLMResponse(
            text_blocks=[reply],
            raw_content=[{"type": "text", "text": reply}],
            stop_reason="end_turn",
            usage={"input_tokens": 20, "output_tokens": 10, "latency_ms": 100},
        )


def test_agent_ask_btw_never_mutates_main_context(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("codelet.plugins.builtin.cockpit.default_usage_path", lambda: tmp_path / "u.jsonl")
    stub = _BtwStubClient()
    monkeypatch.setattr("codelet.agent_loop.build_client", lambda cfg: stub)

    cfg = Config(
        provider=LLMProvider.OPENAI,
        model="test-model",
        permission_mode=PermissionMode.NORMAL,
        plugins={"enabled": ["cockpit"]},
    )
    agent = AgentLoop(cfg, sink=NullSink())

    agent.run("first main question")
    before_msgs = list(agent.context.messages)
    before_len = len(before_msgs)

    chunks: list[str] = []
    ans = asyncio.run(agent.ask_btw("quick side question", on_text=chunks.append))

    assert ans == "side answer"
    assert "".join(chunks) == "side answer"
    # Main context is completely untouched
    assert len(agent.context.messages) == before_len
    assert agent.context.messages == before_msgs
    # Side call ran with tools=[]
    assert stub.calls[-1]["tools"] == []


# ---------------------------------------------------------------------------
# Web backend integration: Cockpit REST/WS, Git Changes/Diff, /btw, /mode, /export, cancel
# ---------------------------------------------------------------------------


class _WebStubClient(LLMClient):
    def __init__(self, *, delay: float = 0.0, use_todo: bool = False) -> None:
        self.delay = delay
        self.use_todo = use_todo
        self.turn = 0

    def chat(self, *, on_text: Any = None, on_reasoning: Any = None, **kwargs: Any) -> LLMResponse:
        if self.delay:
            time.sleep(self.delay)
        if not kwargs.get("tools"):
            if on_text:
                on_text("btw reply")
            return LLMResponse(
                text_blocks=["btw reply"],
                raw_content=[{"type": "text", "text": "btw reply"}],
                stop_reason="end_turn",
                usage={"input_tokens": 15, "output_tokens": 8, "latency_ms": 80},
            )
        self.turn += 1
        if self.use_todo and self.turn == 1:
            tc = ToolCall(
                id="tc_todo",
                name="todo_write",
                input={"todos": [
                    {"content": "Step 1", "status": "completed"},
                    {"content": "Step 2", "status": "in_progress"},
                ]},
            )
            return LLMResponse(
                tool_calls=[tc],
                raw_content=[{"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}],
                stop_reason="tool_use",
                usage={"input_tokens": 30, "output_tokens": 12, "latency_ms": 120},
            )
        if on_reasoning:
            on_reasoning("thinking...")
        if on_text:
            on_text("done!")
        return LLMResponse(
            text_blocks=["done!"],
            reasoning_blocks=["thinking..."],
            raw_content=[{"type": "text", "text": "done!"}],
            stop_reason="end_turn",
            usage={"input_tokens": 40, "output_tokens": 10, "cache_read_tokens": 20, "latency_ms": 100},
        )


def _recv_until(ws, kind: str, limit: int = 30) -> dict[str, Any]:
    for _ in range(limit):
        f = ws.receive_json()
        if f["type"] == kind:
            return f
    raise AssertionError(f"no {kind} frame within {limit}")


def test_web_cockpit_btw_slash_and_git_endpoints(monkeypatch, tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from codelet.llm.models import ModelCatalog
    from codelet.web.server import create_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codelet.web.server.load_env_files", lambda *a, **kw: [])
    monkeypatch.setattr("codelet.plugins.builtin.cockpit.default_usage_path", lambda: tmp_path / "usage.jsonl")
    monkeypatch.delenv("ZENMUX_MANAGEMENT_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MANAGEMENT_API_KEY", raising=False)

    # Enable cockpit in tmp workspace settings
    (tmp_path / ".codelet").mkdir()
    (tmp_path / ".codelet" / "settings.json").write_text(
        '{"plugins": {"enabled": ["cockpit"]}}', encoding="utf-8"
    )
    stub = _WebStubClient(use_todo=True)
    monkeypatch.setattr("codelet.agent_loop.build_client", lambda cfg: stub)

    app = create_app(
        catalog=ModelCatalog(cache_path=tmp_path / "models_cache.json"),
        auto_refresh=False,
    )
    client = TestClient(app)

    # REST /api/cockpit (before WS connect: usage + balance)
    c = client.get("/api/cockpit")
    assert c.status_code == 200
    body = c.json()
    assert body["enabled"] is True
    assert "usage" in body and "balance" in body and body["balance"]["configured"] is False

    # REST /api/git/changes & path-traversal rejection on /api/git/diff
    gc = client.get("/api/git/changes")
    assert gc.status_code == 200
    assert "files" in gc.json()
    bad_diff = client.get("/api/git/diff", params={"path": "../../etc/passwd"})
    assert bad_diff.status_code == 200
    assert bad_diff.json().get("error") == "path outside workspace"

    # WS flows: connect -> cockpit frame -> prompt with todo + reasoning -> /btw -> /mode normal -> /export -> set_context_limit
    with client.websocket_connect("/ws") as ws:
        init_cockpit = _recv_until(ws, "cockpit", limit=15)
        assert init_cockpit["enabled"] is True

        # Prompt triggers todo_write + reasoning_delta + cockpit update
        ws.send_json({"type": "prompt", "text": "do tasks"})
        frames = []
        for _ in range(40):
            f = ws.receive_json()
            frames.append(f)
            if f["type"] == "turn_done":
                break
        ftypes = [f["type"] for f in frames]
        assert "todos" in ftypes
        assert "reasoning_delta" in ftypes
        assert "cockpit" in ftypes

        # /btw opens drawer & streams reply without failing
        ws.send_json({"type": "slash", "line": "/btw what is 2+2?"})
        btw_frames = []
        for _ in range(25):
            f = ws.receive_json()
            btw_frames.append(f)
            if f["type"] == "turn_done":
                break
        btypes = [f["type"] for f in btw_frames]
        assert "ui_action" in btypes
        assert "btw_start" in btypes
        assert "btw_done" in btypes

        # /mode normal
        ws.send_json({"type": "slash", "line": "/mode normal"})
        prof = _recv_until(ws, "profile", limit=10)
        assert prof["mode"] == "normal"
        _recv_until(ws, "turn_done", limit=10)

        # /effort high & set_effort
        ws.send_json({"type": "slash", "line": "/effort high"})
        prof_eff = _recv_until(ws, "profile", limit=10)
        assert prof_eff["reasoning_effort"] == "high"
        _recv_until(ws, "turn_done", limit=10)

        ws.send_json({"type": "set_effort", "effort": "low"})
        prof_eff2 = _recv_until(ws, "profile", limit=10)
        assert prof_eff2["reasoning_effort"] == "low"
        ck_eff = _recv_until(ws, "cockpit", limit=10)
        assert ck_eff["reasoning_effort"] == "low"

        # /export
        ws.send_json({"type": "slash", "line": "/export"})
        exp = _recv_until(ws, "export_markdown", limit=10)
        assert exp["filename"].endswith(".md")
        assert "# codelet conversation" in exp["content"]
        _recv_until(ws, "turn_done", limit=10)

        # set_context_limit
        ws.send_json({"type": "set_context_limit", "limit": 64000})
        updated = _recv_until(ws, "cockpit", limit=10)
        assert updated["context"]["limit"] == 64000


def test_web_cancel_in_flight_turn(monkeypatch, tmp_path: Path):
    """Sending `cancel` while awaiting an ASK approval cancels the turn cleanly."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from codelet.llm.models import ModelCatalog
    from codelet.web.server import create_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codelet.web.server.load_env_files", lambda *a, **kw: [])
    monkeypatch.setattr("codelet.plugins.builtin.cockpit.default_usage_path", lambda: tmp_path / "usage.jsonl")

    target = str(tmp_path / "should_not_exist.txt")

    class _WriteClient(LLMClient):
        def chat(self, **kwargs: Any) -> LLMResponse:
            tc = ToolCall(id="w_cancel", name="write_file", input={"path": target, "content": "nope"})
            return LLMResponse(
                tool_calls=[tc],
                raw_content=[{"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}],
                stop_reason="tool_use",
                usage={"input_tokens": 10, "output_tokens": 5},
            )

    monkeypatch.setattr("codelet.agent_loop.build_client", lambda cfg: _WriteClient())
    app = create_app(
        catalog=ModelCatalog(cache_path=tmp_path / "models_cache.json"),
        auto_refresh=False,
    )
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        _recv_until(ws, "cockpit", limit=15)
        ws.send_json({"type": "set_mode", "mode": "ask"})
        _recv_until(ws, "profile", limit=10)
        ws.send_json({"type": "prompt", "text": "write file"})
        _recv_until(ws, "permission_request", limit=15)
        ws.send_json({"type": "cancel"})
        frames = []
        for _ in range(20):
            f = ws.receive_json()
            frames.append(f)
            if f["type"] == "turn_done":
                break
        ftypes = [f["type"] for f in frames]
        assert "turn_cancelled" in ftypes
        assert not (tmp_path / "should_not_exist.txt").exists()


# ---------------------------------------------------------------------------
# 2-layer capability architecture: Plugin-bundled skills, directory skill packs,
# and 3-way classification for newly installed skills
# ---------------------------------------------------------------------------


def test_skill_three_way_classification_and_directory_pack(monkeypatch, tmp_path: Path):
    from codelet.plugins.loader import discover_plugins
    from codelet.skills.loader import load_skills

    proj_skills = tmp_path / ".codelet" / "skills"
    user_skills = tmp_path / "home_skills"
    proj_skills.mkdir(parents=True)
    user_skills.mkdir(parents=True)

    # 1. Standalone project skill with optional category
    (proj_skills / "lint.md").write_text(
        "---\nname: my-lint\ndescription: Run lint checks\ncategory: review\n---\n# body\n",
        encoding="utf-8",
    )
    # 2. Standalone global skill with no category (zero-config fallback)
    (user_skills / "notes.md").write_text(
        "---\nname: quick-notes\ndescription: Take notes\n---\n# body\n",
        encoding="utf-8",
    )
    # 3. Standalone skill explicitly attached to `memory` plugin via frontmatter
    (proj_skills / "mem-ext.md").write_text(
        "---\nname: mem-extra\ndescription: Extra memory workflow\nplugin: memory\ncategory: memory\n---\n# body\n",
        encoding="utf-8",
    )
    # 4. Pure directory skill-pack plugin (no Python code required)
    pack_dir = tmp_path / ".codelet" / "plugins" / "qa-pack" / "skills"
    pack_dir.mkdir(parents=True)
    (pack_dir / "smoke.md").write_text(
        "---\nname: smoke-test\ndescription: Smoke test checklist\ncategory: testing\n---\n# body\n",
        encoding="utf-8",
    )

    discovered = discover_plugins(plugin_dirs=[tmp_path / ".codelet" / "plugins"])
    assert any(p.name == "qa-pack" for p in discovered)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codelet.agent_loop.build_client", lambda cfg: _BtwStubClient())

    # Without `memory` enabled, `mem-extra` stays hidden; `qa-pack` enabled loads `smoke-test`
    idx1 = load_skills(project_dir=tmp_path, user_dir=user_skills)
    cfg1 = Config(provider=LLMProvider.OPENAI, model="test", plugins={"enabled": ["qa-pack"]})
    agent1 = AgentLoop(cfg1, sink=NullSink(), skill_index=idx1)
    by_name1 = {s["name"]: s for s in agent1.skills_state()}
    assert by_name1["my-lint"]["scope"] == "project" and by_name1["my-lint"]["category"] == "review"
    assert by_name1["quick-notes"]["scope"] == "global" and by_name1["quick-notes"]["plugin"] is None
    assert by_name1["smoke-test"]["plugin"] == "qa-pack"
    assert "mem-extra" not in by_name1

    # With `memory` enabled, both bundled `memory-hygiene` and frontmatter-attached `mem-extra` appear
    idx2 = load_skills(project_dir=tmp_path, user_dir=user_skills)
    cfg2 = Config(provider=LLMProvider.OPENAI, model="test", plugins={"enabled": ["memory"]})
    agent2 = AgentLoop(cfg2, sink=NullSink(), skill_index=idx2)
    by_name2 = {s["name"]: s for s in agent2.skills_state()}
    assert by_name2["memory-hygiene"]["plugin"] == "memory"
    assert by_name2["mem-extra"]["plugin"] == "memory"


def test_web_plugin_toggle_hot_plugs_bundled_skills(monkeypatch, tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from codelet.llm.models import ModelCatalog
    from codelet.web.server import create_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codelet.web.server.load_env_files", lambda *a, **kw: [])
    monkeypatch.setattr("codelet.plugins.builtin.cockpit.default_usage_path", lambda: tmp_path / "u.jsonl")
    monkeypatch.setattr("codelet.agent_loop.build_client", lambda cfg: _BtwStubClient())

    app = create_app(
        catalog=ModelCatalog(cache_path=tmp_path / "models_cache.json"),
        auto_refresh=False,
    )
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        _recv_until(ws, "cockpit", limit=15)
        ws.send_json({"type": "set_plugin_enabled", "name": "memory", "enabled": True})
        p_on = _recv_until(ws, "plugins", limit=10)["plugins"]
        _recv_until(ws, "tools", limit=10)
        s_on = _recv_until(ws, "skills", limit=10)["skills"]
        mem_plugin = next(p for p in p_on if p["name"] == "memory")
        assert "memory-hygiene" in mem_plugin["skills"]
        assert any(s["name"] == "memory-hygiene" and s["plugin"] == "memory" for s in s_on)

        ws.send_json({"type": "set_plugin_enabled", "name": "memory", "enabled": False})
        _recv_until(ws, "plugins", limit=10)
        _recv_until(ws, "tools", limit=10)
        s_off = _recv_until(ws, "skills", limit=10)["skills"]
        assert not any(s["name"] == "memory-hygiene" for s in s_off)


