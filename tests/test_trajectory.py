"""The trajectory records each step faithfully and never breaks the loop."""
from __future__ import annotations

import json

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.llm.base import LLMResponse, ToolCall
from codelet.tools.base import ToolRegistry
from codelet.trajectory import OUTPUT_CAP, Trajectory, clip, load, mask

from tests.test_agent_loop import EchoTool, ScriptedClient, _text_response, _tool_response


def _agent(client, mode=PermissionMode.AUTO, trajectory=None):
    reg = ToolRegistry()
    reg.register(EchoTool())
    return AgentLoop(config=Config(permission_mode=mode), registry=reg, client=client,
                     trajectory=trajectory)


def test_steps_are_recorded_in_order(tmp_path):
    path = tmp_path / "t.jsonl"
    trace = Trajectory(path, meta={"task": "demo"})
    client = ScriptedClient([
        _tool_response([ToolCall(id="a", name="echo", input={"msg": "1"}),
                        ToolCall(id="b", name="echo", input={"msg": "2"})]),
        _text_response("done"),
    ])
    _agent(client, trajectory=trace).run("go")

    rows = load(path)
    assert [r["kind"] for r in rows] == [
        "run_start", "user_turn", "llm_call", "tool_call", "tool_call", "llm_call", "turn_end"]
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    llm1, t_a, t_b, llm2 = rows[2], rows[3], rows[4], rows[5]
    assert llm1["step"] == 1 and llm2["step"] == 2
    assert [c["name"] for c in llm1["tool_calls"]] == ["echo", "echo"]
    # A tool call carries the step of the LLM call that asked for it.
    assert {t_a["step"], t_b["step"]} == {1}
    assert {t_a["tool_use_id"], t_b["tool_use_id"]} == {"a", "b"}
    assert t_a["batch"] == 2 and t_a["outcome"] == "ok"
    assert rows[-1]["llm_calls"] == 2 and rows[-1]["max_turns_hit"] is False
    assert rows == trace.events                        # file and memory agree


def test_gate_outcomes_are_named_not_lumped_as_errors():
    trace = Trajectory()
    client = ScriptedClient([
        _tool_response([ToolCall(id="x", name="no_such_tool", input={})]),
        _text_response("ok"),
    ])
    _agent(client, trajectory=trace).run("go")
    call = next(e for e in trace.events if e["kind"] == "tool_call")
    assert call["outcome"] == "unknown_tool"
    assert call["is_error"] is True


def test_max_turns_is_visible_in_the_trace():
    trace = Trajectory()
    loop_forever = [_tool_response([ToolCall(id=f"c{i}", name="echo", input={"msg": "x"})])
                    for i in range(10)]
    agent = _agent(ScriptedClient(loop_forever), trajectory=trace)
    agent.config.max_turns = 3
    agent.run("go")
    end = trace.events[-1]
    assert end["kind"] == "turn_end" and end["max_turns_hit"] is True
    assert end["llm_calls"] == 3


def test_secrets_are_masked_and_long_outputs_clipped():
    assert "sk-" not in mask("key sk-abcdefghijklmnop1234 here")
    assert mask("password=hunter2 rest") == "[REDACTED] rest"
    long = "y" * (OUTPUT_CAP + 50)
    out = clip(long, OUTPUT_CAP)
    assert out.endswith("…[+50 chars]") and len(out) < len(long)


def test_tracing_off_by_default_and_failures_never_raise(tmp_path):
    agent = _agent(ScriptedClient([_text_response("hi")]))
    assert agent.trajectory is None
    assert agent.run("go") == "hi"

    # An unwritable path costs the trace, not the run.
    bad = Trajectory(tmp_path / "t.jsonl")
    bad.path = tmp_path                                 # a directory: open() fails
    assert _agent(ScriptedClient([_text_response("hi")]), trajectory=bad).run("go") == "hi"


def test_child_view_shares_sequence_and_file(tmp_path):
    path = tmp_path / "t.jsonl"
    parent = Trajectory(path)
    child = parent.child("sub1:general")
    parent.record("a")
    child.record("b")
    parent.record("c")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [(r["agent"], r["seq"]) for r in rows] == [("main", 1), ("sub1:general", 2), ("main", 3)]
