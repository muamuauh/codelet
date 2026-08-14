"""Plugin system: discovery/apply collects contributions, a broken plugin is
skipped, and the loop actually runs prompt + tool middleware."""
from __future__ import annotations

from typing import Any

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.events import RecordingSink
from codelet.llm.base import LLMClient, LLMResponse, ToolCall
from codelet.plugins.base import PluginContext
from codelet.plugins.loader import apply_plugins
from codelet.tools.base import Tool, ToolRegistry, ToolResult


# ---------- fakes ----------

class ScriptedClient(LLMClient):
    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)

    def chat(self, *, on_text: Any = None, **kw: Any) -> LLMResponse:
        return self.script.pop(0) if self.script else LLMResponse(
            text_blocks=["(end)"], raw_content=[{"type": "text", "text": "(end)"}], stop_reason="end_turn")


class EchoTool(Tool):
    @property
    def name(self) -> str: return "echo"
    @property
    def description(self) -> str: return "echo"
    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"msg": {"type": "string"}}}
    def execute(self, params: dict[str, Any]) -> ToolResult:
        return ToolResult(output=f"echo:{params.get('msg', '')}")


class PingTool(Tool):
    @property
    def name(self) -> str: return "ping"
    @property
    def description(self) -> str: return "ping"
    @property
    def input_schema(self) -> dict[str, Any]: return {"type": "object", "properties": {}}
    def execute(self, params: dict[str, Any]) -> ToolResult: return ToolResult(output="pong")


class GoodPlugin:
    name = "good"
    def setup(self, ctx: PluginContext) -> None:
        ctx.register_tool(PingTool())
        ctx.add_system_prompt_section("PLUGIN SECTION")
        ctx.on_user_prompt(lambda t: t + " [mw]")
        async def wrap(name, tool_input, call_next):
            r = await call_next()
            return ToolResult(output=r.output + " [wrapped]", is_error=r.is_error)
        ctx.wrap_tool(wrap)


class BrokenPlugin:
    name = "broken"
    def setup(self, ctx: PluginContext) -> None:
        raise RuntimeError("boom")


def _text(t): return LLMResponse(text_blocks=[t], raw_content=[{"type": "text", "text": t}], stop_reason="end_turn")
def _tools(calls):
    raw = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
    return LLMResponse(tool_calls=calls, raw_content=raw, stop_reason="tool_use")


# ---------- loader ----------

def test_apply_plugins_collects_contributions():
    reg = ToolRegistry()
    applied = apply_plugins(reg, {}, extra=[GoodPlugin()], plugin_dirs=[])
    assert "good" in applied.names
    assert reg.get("ping") is not None                 # tool registered
    assert "PLUGIN SECTION" in applied.prompt_sections
    assert len(applied.prompt_middleware) == 1
    assert len(applied.tool_middleware) == 1


def test_broken_plugin_is_skipped():
    reg = ToolRegistry()
    applied = apply_plugins(reg, {}, extra=[BrokenPlugin(), GoodPlugin()], plugin_dirs=[])
    assert "broken" not in applied.names
    assert "good" in applied.names                     # the good one still applied


def test_enabled_allowlist_filters():
    reg = ToolRegistry()
    applied = apply_plugins(reg, {"enabled": []}, extra=[GoodPlugin()], plugin_dirs=[])
    assert applied.names == []                          # allowlist excludes it


# ---------- loop wiring ----------

def _agent(client, sink):
    reg = ToolRegistry()
    reg.register(EchoTool())
    return AgentLoop(config=Config(permission_mode=PermissionMode.AUTO), registry=reg, client=client, sink=sink)


def test_prompt_middleware_transforms_the_prompt():
    agent = _agent(ScriptedClient([_text("ok")]), RecordingSink())
    agent._prompt_middleware = [lambda t: t + " [mw]"]
    agent.run("hello")
    first_user = [m for m in agent.context.messages if m["role"] == "user"][0]
    assert first_user["content"] == "hello [mw]"


def test_tool_middleware_wraps_execution():
    async def wrap(name, tool_input, call_next):
        r = await call_next()
        return ToolResult(output=r.output + " [wrapped]", is_error=r.is_error)
    sink = RecordingSink()
    agent = _agent(ScriptedClient([_tools([ToolCall("t1", "echo", {"msg": "hi"})]), _text("done")]), sink)
    agent._tool_middleware = [wrap]
    agent.run("go")
    assert any(e[0] == "tool_result" and "echo:hi [wrapped]" in e[2] for e in sink.events)


def test_run_command_dispatches_to_plugin():
    agent = _agent(ScriptedClient([]), RecordingSink())
    agent._plugin_commands = {"greet": lambda args: f"hi {args}"}
    assert agent.run_command("greet", "there") == "hi there"
    assert agent.run_command("/greet", "x") == "hi x"     # leading slash tolerated
    assert agent.run_command("nope") is None              # unknown -> None


def test_subagent_inherits_plugin_middleware():
    parent = _agent(ScriptedClient([]), RecordingSink())
    marker = object()
    parent._tool_middleware = [marker]
    parent._prompt_middleware = [lambda t: t]
    parent._plugin_commands = {"cmd": lambda a: "ok"}
    parent._prompt_sections = ["SECTION"]

    child = AgentLoop(config=Config(permission_mode=PermissionMode.AUTO),
                      registry=ToolRegistry(), client=ScriptedClient([]),
                      sink=RecordingSink(), _is_subagent=True)
    assert child._tool_middleware == []                    # subagents don't self-apply
    child.inherit_plugins_from(parent)
    assert child._tool_middleware == [marker]
    assert child._prompt_sections == ["SECTION"]
    assert child._plugin_commands["cmd"]("") == "ok"
