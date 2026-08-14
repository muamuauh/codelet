"""Self-evolution (`evolve` plugin): the agent authors a tool with `create_tool`,
it is written to a plugin file, hot-activated into the live loop, callable
immediately, and reloaded on the next startup."""
from __future__ import annotations

from typing import Any

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.events import RecordingSink
from codelet.llm.base import LLMClient, LLMResponse
from codelet.plugins.builtin.evolve import render_tool_source
from codelet.tools.base import ToolRegistry


class ScriptedClient(LLMClient):
    def chat(self, *, on_text: Any = None, **kw: Any) -> LLMResponse:
        return LLMResponse(text_blocks=["(end)"],
                           raw_content=[{"type": "text", "text": "(end)"}],
                           stop_reason="end_turn")


def _agent(tmp_path) -> AgentLoop:
    cfg = Config(
        permission_mode=PermissionMode.AUTO,
        plugins={"enabled": ["evolve"], "config": {"evolve": {"dir": str(tmp_path / "evolved")}}},
    )
    return AgentLoop(config=cfg, registry=ToolRegistry(), client=ScriptedClient(),
                     sink=RecordingSink())


# ---------- source rendering ----------

def test_render_produces_importable_module():
    src = render_tool_source(
        "greet", "Greet someone.",
        {"type": "object", "properties": {"who": {"type": "string"}}, "required": ["who"]},
        'return "hello " + params["who"]',
    )
    compile(src, "<test>", "exec")               # syntactically valid
    assert "class GreetTool(Tool)" in src
    assert "return 'greet'" in src
    assert 'PLUGIN = _Plugin()' in src


def test_render_defaults_empty_body_to_none():
    src = render_tool_source("noop", "does nothing", {}, "   ")
    compile(src, "<test>", "exec")
    assert "return None" in src


# ---------- live round-trip ----------

def test_create_tool_registers_and_activates(tmp_path):
    agent = _agent(tmp_path)
    create = agent.registry.get("create_tool")
    assert create is not None

    res = create.execute({
        "name": "greet",
        "description": "Greet someone by name.",
        "parameters": {"type": "object", "properties": {"who": {"type": "string"}},
                       "required": ["who"]},
        "code": 'return "hello " + params["who"]',
    })
    assert not res.is_error, res.output

    # The new tool is live in the registry and callable right away.
    greet = agent.registry.get("greet")
    assert greet is not None
    assert greet.execute({"who": "world"}).output == "hello world"

    # And it made it into the rebuilt system prompt the model will see next turn.
    assert "greet" in agent.context.system_prompt
    # And it was persisted as a file.
    assert (tmp_path / "evolved" / "greet.py").exists()


def test_evolved_tool_reloads_on_next_startup(tmp_path):
    agent = _agent(tmp_path)
    agent.registry.get("create_tool").execute({
        "name": "shout",
        "description": "Uppercase text.",
        "parameters": {"properties": {"text": {"type": "string"}}},
        "code": 'return params["text"].upper()',
    })

    # A brand-new loop over the same evolved dir picks the tool back up.
    fresh = _agent(tmp_path)
    tool = fresh.registry.get("shout")
    assert tool is not None
    assert tool.execute({"text": "hi"}).output == "HI"


def test_bare_properties_map_is_accepted(tmp_path):
    agent = _agent(tmp_path)
    res = agent.registry.get("create_tool").execute({
        "name": "add",
        "description": "add two numbers",
        "parameters": {"a": {"type": "number"}, "b": {"type": "number"}},  # no wrapping "type"
        "code": 'return str(params["a"] + params["b"])',
    })
    assert not res.is_error
    assert agent.registry.get("add").execute({"a": 2, "b": 3}).output == "5"


# ---------- validation / safety ----------

def test_protected_name_is_refused(tmp_path):
    agent = _agent(tmp_path)
    res = agent.registry.get("create_tool").execute({
        "name": "bash", "description": "x", "code": "return 'x'"})
    assert res.is_error and "protected" in res.output


def test_invalid_identifier_is_refused(tmp_path):
    agent = _agent(tmp_path)
    res = agent.registry.get("create_tool").execute({
        "name": "9bad-name", "description": "x", "code": "return 'x'"})
    assert res.is_error and "identifier" in res.output


def test_existing_tool_requires_overwrite(tmp_path):
    agent = _agent(tmp_path)
    args = {"name": "dup", "description": "d", "code": "return '1'"}
    assert not agent.registry.get("create_tool").execute(args).is_error
    again = agent.registry.get("create_tool").execute(args)
    assert again.is_error and "overwrite" in again.output
    # ...and with overwrite it succeeds.
    ok = agent.registry.get("create_tool").execute({**args, "overwrite": True})
    assert not ok.is_error


def test_syntax_error_in_body_is_reported(tmp_path):
    agent = _agent(tmp_path)
    res = agent.registry.get("create_tool").execute({
        "name": "broken", "description": "x", "code": "return ("})  # unbalanced
    assert res.is_error and "compile" in res.output.lower()
    # Nothing half-written should have been registered.
    assert agent.registry.get("broken") is None


def test_ask_mode_preview_shows_source(tmp_path):
    agent = _agent(tmp_path)
    preview = agent.registry.get("create_tool").preview_diff({
        "name": "peek", "description": "d",
        "parameters": {"properties": {"x": {"type": "string"}}},
        "code": "return params['x']"})
    assert preview and "class PeekTool(Tool)" in preview


def test_evolve_command_lists_tools(tmp_path):
    agent = _agent(tmp_path)
    assert "no evolved tools" in agent.run_command("evolve", "")
    agent.registry.get("create_tool").execute({
        "name": "listed", "description": "d", "code": "return '1'"})
    assert "listed" in agent.run_command("evolve", "")
