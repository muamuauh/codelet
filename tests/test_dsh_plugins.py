"""Tests for DSH/Cordis-inspired plugin architecture upgrades:
  1. Reversible PluginHandle.dispose(), ctx.effect(), overridden-tool restoration, rollback on error
  2. `requires` topological sort, cross-plugin `provide`/`service`, cycle/missing skip, cascade unmount
  3. Built-in `mcp` bridge plugin (real local stdio MCP server subprocess + clean dispose)
  4. Creator workflow (`create_plugin_pack` hot-mount + built-in `plugin-creator` skill)
  5. Web in-place hot-plug (no AgentLoop rebuild) + dynamic plugin UI panels (`panels` / `render_panel`)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from codelet.agent_loop import AgentLoop
from codelet.config import Config, LLMProvider, PermissionMode
from codelet.events import NullSink
from codelet.llm.base import LLMClient, LLMResponse
from codelet.plugins.base import PluginContext
from codelet.plugins.builtin.evolve import CreatePluginPackTool
from codelet.plugins.loader import apply_plugins
from codelet.skills.loader import load_skills
from codelet.tools.base import Tool, ToolRegistry, ToolResult


class _StubClient(LLMClient):
    def chat(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            text_blocks=["ok"],
            raw_content=[{"type": "text", "text": "ok"}],
            stop_reason="end_turn",
            usage={"input_tokens": 5, "output_tokens": 3},
        )


class _SimpleTool(Tool):
    read_only = True

    def __init__(self, name: str, out: str) -> None:
        self._name = name
        self._out = out

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        return ToolResult(output=self._out)


# ---------------------------------------------------------------------------
# 1. Reversible PluginHandle.dispose(), ctx.effect(), rollback & tool restore
# ---------------------------------------------------------------------------


def test_reversible_dispose_restores_overridden_tool_and_runs_effects():
    reg = ToolRegistry()
    reg.register(_SimpleTool("core_x", "original"))
    cleaned: list[str] = []

    class OverridePlugin:
        name = "ov"

        def setup(self, ctx: PluginContext) -> None:
            ctx.register_tool(_SimpleTool("core_x", "overridden"))
            ctx.register_tool(_SimpleTool("extra_y", "extra"))
            ctx.provide("ov_svc", {"v": 42})
            ctx.effect(lambda: (lambda: cleaned.append("effect_ran")))
            ctx.on_dispose(lambda: cleaned.append("dispose_ran"))

    applied = apply_plugins(reg, {"enabled": ["ov"]}, extra=[OverridePlugin()], plugin_dirs=[])
    assert reg.get("core_x").execute({}).output == "overridden"
    assert reg.get("extra_y") is not None
    assert applied.services["ov_svc"] == {"v": 42}

    unmounted = applied.unmount("ov", reg)
    assert unmounted == ["ov"]
    # Overridden core tool restored verbatim, extra tool removed, service cleared, disposers ran LIFO.
    assert reg.get("core_x").execute({}).output == "original"
    assert reg.get("extra_y") is None
    assert "ov_svc" not in applied.services
    assert cleaned == ["dispose_ran", "effect_ran"]


def test_failed_setup_rolls_back_partial_registrations():
    reg = ToolRegistry()
    rolled_back: list[bool] = []

    class HalfBroken:
        name = "half"

        def setup(self, ctx: PluginContext) -> None:
            ctx.register_tool(_SimpleTool("leaked", "no"))
            ctx.on_dispose(lambda: rolled_back.append(True))
            raise RuntimeError("mid-setup crash")

    applied = apply_plugins(reg, {"enabled": ["half"]}, extra=[HalfBroken()], plugin_dirs=[])
    assert "half" not in applied.names
    assert "half" in applied.skipped
    assert reg.get("leaked") is None
    assert rolled_back == [True]


# ---------------------------------------------------------------------------
# 2. `requires` topological sort, services, cycle/missing skip & cascade unmount
# ---------------------------------------------------------------------------


def test_requires_topo_sort_services_and_cascade_unmount():
    reg = ToolRegistry()
    mount_order: list[str] = []
    dispose_order: list[str] = []

    class BasePlugin:
        name = "base"

        def setup(self, ctx: PluginContext) -> None:
            mount_order.append("base")
            ctx.provide("greeter", lambda who: f"hello {who}")
            ctx.on_dispose(lambda: dispose_order.append("base"))

    class ChildPlugin:
        name = "child"
        requires = ("base",)

        def setup(self, ctx: PluginContext) -> None:
            mount_order.append("child")
            greeter = ctx.service("greeter")
            assert greeter is not None
            ctx.register_tool(_SimpleTool("child_greet", greeter("world")))
            ctx.on_dispose(lambda: dispose_order.append("child"))

    class OrphanPlugin:
        name = "orphan"
        requires = ("missing_dep",)

        def setup(self, ctx: PluginContext) -> None:
            mount_order.append("orphan")

    # Pass `child` BEFORE `base` to prove topological sort reorders them.
    applied = apply_plugins(
        reg,
        {"enabled": ["child", "base", "orphan"]},
        extra=[BasePlugin(), ChildPlugin(), OrphanPlugin()],
        plugin_dirs=[],
    )
    assert mount_order == ["base", "child"]
    assert "orphan" in applied.skipped and "missing_dep" in applied.skipped["orphan"]
    assert reg.get("child_greet").execute({}).output == "hello world"

    # Unmounting `base` must cascade-unmount `child` first!
    unmounted = applied.unmount("base", reg)
    assert unmounted == ["child", "base"]
    assert dispose_order == ["child", "base"]
    assert reg.get("child_greet") is None


def test_circular_requires_detected_and_skipped():
    class A:
        name = "a"
        requires = ("b",)
        def setup(self, ctx: PluginContext) -> None: pass

    class B:
        name = "b"
        requires = ("a",)
        def setup(self, ctx: PluginContext) -> None: pass

    applied = apply_plugins(ToolRegistry(), {"enabled": ["a", "b"]}, extra=[A(), B()], plugin_dirs=[])
    assert applied.names == []
    assert "a" in applied.skipped and "b" in applied.skipped


# ---------------------------------------------------------------------------
# 3. Native MCP bridge plugin end-to-end with real stdio JSON-RPC subprocess
# ---------------------------------------------------------------------------


_FAKE_MCP_SERVER = """\
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    rid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "calc-demo", "version": "1.0"},
            },
        }
        print(json.dumps(resp), flush=True)
    elif method == "tools/list":
        resp = {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "tools": [{
                    "name": "add_numbers",
                    "description": "Add two integers",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                        "required": ["a", "b"],
                    },
                    "annotations": {"readOnlyHint": True},
                }]
            },
        }
        print(json.dumps(resp), flush=True)
    elif method == "tools/call":
        args = msg.get("params", {}).get("arguments", {})
        total = int(args.get("a", 0)) + int(args.get("b", 0))
        resp = {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"content": [{"type": "text", "text": str(total)}], "isError": False},
        }
        print(json.dumps(resp), flush=True)
"""


def test_mcp_plugin_stdio_bridge_panel_and_clean_dispose(monkeypatch, tmp_path: Path):
    srv_script = tmp_path / "fake_mcp.py"
    srv_script.write_text(_FAKE_MCP_SERVER, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codelet.agent_loop.build_client", lambda cfg: _StubClient())

    cfg = Config(
        provider=LLMProvider.OPENAI,
        model="test",
        plugins={
            "enabled": ["mcp"],
            "config": {
                "mcp": {
                    "servers": {
                        "calc": {
                            "transport": "stdio",
                            "command": [sys.executable, str(srv_script)],
                        }
                    }
                }
            },
        },
    )
    agent = AgentLoop(cfg, sink=NullSink())
    try:
        tool = agent.registry.get("mcp__calc__add_numbers")
        assert tool is not None
        assert tool.read_only is True  # respected readOnlyHint
        res = tool.execute({"a": 19, "b": 23})
        assert not res.is_error and res.output == "42"

        # Dynamic panel registered & renders live status
        panels = agent.panels_state()
        assert any(p["id"] == "mcp-servers" for p in panels)
        rendered = agent.render_panel("mcp-servers")
        assert rendered is not None and rendered["sections"][0]["title"] == "calc"

        # /mcp slash command reports connected server
        cmd_out = agent.run_command("mcp", "")
        assert cmd_out and "calc" in cmd_out and "connected" in cmd_out

        # Unmounting cleanly removes tool, panel, skill, and terminates subprocess
        unmounted = agent.unmount_plugin("mcp")
        assert unmounted == ["mcp"]
        assert agent.registry.get("mcp__calc__add_numbers") is None
        assert agent.panels_state() == []
    finally:
        agent._applied_plugins.dispose_all(agent.registry)


# ---------------------------------------------------------------------------
# 4. Creator workflow (`create_plugin_pack` + built-in `plugin-creator`)
# ---------------------------------------------------------------------------


def test_creator_plugin_pack_hot_mounts_and_unmounts(monkeypatch, tmp_path: Path):
    idx = load_skills(project_dir=tmp_path, user_dir=tmp_path / "u")
    assert idx.get("plugin-creator") is not None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codelet.agent_loop.build_client", lambda cfg: _StubClient())

    cfg = Config(provider=LLMProvider.OPENAI, model="test", plugins={"enabled": ["evolve"]})
    agent = AgentLoop(cfg, sink=NullSink())

    pack_tool = agent.registry.get("create_plugin_pack")
    assert isinstance(pack_tool, CreatePluginPackTool)
    res = pack_tool.execute({
        "name": "demo-kit",
        "description": "Demo Creator pack",
        "command_name": "demokit",
        "command_reply": "demo-kit ready",
        "tools": [{
            "name": "shout_text",
            "description": "Uppercase text",
            "parameters": {"properties": {"s": {"type": "string"}}, "required": ["s"]},
            "code": "return params['s'].upper()",
            "read_only": True,
        }],
        "skills": [{
            "name": "demo-flow",
            "description": "How to use demo-kit",
            "category": "demo",
            "body": "Always call `shout_text`.",
        }],
    })
    assert not res.is_error, res.output

    # Immediately hot-mounted into live AgentLoop!
    assert agent.registry.get("shout_text").execute({"s": "hi"}).output == "HI"
    assert agent.skill_index.get("demo-flow") is not None
    assert agent.run_command("demokit", "") == "demo-kit ready"

    # Unmounts cleanly in place
    assert agent.unmount_plugin("demo-kit") == ["demo-kit"]
    assert agent.registry.get("shout_text") is None
    assert agent.skill_index.get("demo-flow") is None


# ---------------------------------------------------------------------------
# 5. Web in-place hot-plug (same AgentLoop instance!) + dynamic plugin panels
# ---------------------------------------------------------------------------


def test_web_inplace_hot_plug_and_dynamic_panel_frames(monkeypatch, tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from codelet.llm.models import ModelCatalog
    from codelet.web.server import create_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codelet.web.server.load_env_files", lambda *a, **kw: [])
    monkeypatch.setattr("codelet.plugins.builtin.cockpit.default_usage_path", lambda: tmp_path / "u.jsonl")
    monkeypatch.setattr("codelet.agent_loop.build_client", lambda cfg: _StubClient())

    app = create_app(
        catalog=ModelCatalog(cache_path=tmp_path / "models_cache.json"),
        auto_refresh=False,
    )
    client = TestClient(app)

    def _recv_until(ws, kind: str, limit: int = 15) -> dict[str, Any]:
        for _ in range(limit):
            f = ws.receive_json()
            if f["type"] == kind:
                return f
        raise AssertionError(f"no {kind} frame within {limit}")

    with client.websocket_connect("/ws") as ws:
        _recv_until(ws, "cockpit", limit=15)

        # Toggle `mcp` on -> pushes `panels` with `mcp-servers`
        ws.send_json({"type": "set_plugin_enabled", "name": "mcp", "enabled": True})
        panels_on = _recv_until(ws, "panels", limit=15)["panels"]
        assert any(p["id"] == "mcp-servers" for p in panels_on)

        # Request rendered panel over WS
        ws.send_json({"type": "render_panel", "id": "mcp-servers"})
        pdata = _recv_until(ws, "panel_data", limit=10)
        assert pdata["id"] == "mcp-servers"
        assert isinstance(pdata.get("sections"), list)

        # REST /api/panels also reflects active panel
        r_panels = client.get("/api/panels")
        assert r_panels.status_code == 200 and any(p["id"] == "mcp-servers" for p in r_panels.json())

        # Toggle `mcp` off -> `panels` becomes empty without rebuilding session
        ws.send_json({"type": "set_plugin_enabled", "name": "mcp", "enabled": False})
        panels_off = _recv_until(ws, "panels", limit=15)["panels"]
        assert not any(p["id"] == "mcp-servers" for p in panels_off)
