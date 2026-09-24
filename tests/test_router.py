"""Intent router (P3): questions, plans and vague requests run read-only.

The contract: restrict only on a confident call, fall through to the unrouted
behaviour otherwise, and let a short confirmation turn changes back on.
"""
from __future__ import annotations

from typing import Any

import pytest

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.llm.base import LLMClient, LLMResponse, ToolCall
from codelet.plugins.base import TurnPolicy
from codelet.plugins.builtin.router import classify
from codelet.tools.base import ToolRegistry
from codelet.tools.file_read import FileReadTool
from codelet.tools.file_write import FileWriteTool


@pytest.mark.parametrize("text, label", [
    ("calc.py 里的 divide 是做什么的？", "question"),
    ("Explain how the retry decorator works", "question"),
    ("为什么这里用 gather 而不是 TaskGroup", "question"),
    ("Where is the config loaded?", "question"),
    ("能帮我把 timeout 改成 30 秒吗？", "edit"),            # polite request phrased as a question
    ("Can you add tests for utils.py?", "edit"),
    ("把 parse_cfg 重命名为 load_config", "edit"),
    ("跑一下测试", "command"),
    ("Why is the build failing?", "command"),                # needs running, not read-only
    ("先别改代码，给我一个重构方案", "plan"),                  # plan beats the edit verb
    ("How should we split this module?", "plan"),
    ("优化一下", "unclear"),
    ("fix it", "unclear"),
    ("hello there", None),                                   # no signal: unrouted
])
def test_classify(text, label):
    assert classify(text)[0] == label


def test_a_confirmation_only_counts_after_a_restricted_turn():
    assert classify("好的，改吧", after_restricted=True)[0] == "edit"
    assert classify("go ahead", after_restricted=True)[0] == "edit"
    assert classify("好的", after_restricted=False)[0] is None


# ---------- through the loop ----------

class _Scripted(LLMClient):
    def __init__(self, script: list[LLMResponse]) -> None:
        self.script, self.calls = list(script), []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return self.script.pop(0) if self.script else LLMResponse(
            text_blocks=["ok"], raw_content=[{"type": "text", "text": "ok"}], stop_reason="end_turn")


def _write(path: str) -> LLMResponse:
    call = ToolCall(id=f"toolu_{path}", name="write_file", input={"path": path, "content": "x"})
    return LLMResponse(tool_calls=[call], stop_reason="tool_use", raw_content=[
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}])


def _agent(client, tmp_path) -> AgentLoop:
    reg = ToolRegistry()
    reg.register(FileWriteTool())
    reg.register(FileReadTool())
    cfg = Config(permission_mode=PermissionMode.AUTO, plugins={"enabled": ["router"]})
    return AgentLoop(config=cfg, registry=reg, client=client)


def _last_result(client) -> str:
    return client.calls[-1]["messages"][-1]["content"][0]["content"]


def test_question_turn_is_read_only_until_confirmed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _Scripted([_write("fixed.py")])
    agent = _agent(client, tmp_path)
    agent.run("calc.py 里的 divide 是做什么的？")
    assert agent._turn.label == "question" and agent._turn.read_only
    assert not (tmp_path / "fixed.py").exists()
    assert "routed as 'question'" in _last_result(client)
    assert "<turn_note>Routed as a question" in client.calls[0]["messages"][0]["content"]

    client.script = [_write("fixed.py")]
    agent.run("好的，改吧")
    assert agent._turn.label == "edit" and not agent._turn.read_only
    assert (tmp_path / "fixed.py").exists()


def test_edit_turn_runs_as_if_unrouted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _Scripted([_write("new.py")])
    agent = _agent(client, tmp_path)
    agent.run("创建一个 new.py")
    assert (tmp_path / "new.py").exists()


def test_a_failing_policy_leaves_the_turn_unrouted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _Scripted([_write("x.py")])
    agent = _agent(client, tmp_path)

    def broken(text: str) -> TurnPolicy:
        raise RuntimeError("classifier down")

    agent._turn_policies = [broken]
    agent.run("what is this?")
    assert agent._turn is None and (tmp_path / "x.py").exists()


def test_router_is_off_unless_enabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _Scripted([_write("x.py")])
    reg = ToolRegistry()
    reg.register(FileWriteTool())
    agent = AgentLoop(config=Config(permission_mode=PermissionMode.AUTO), registry=reg, client=client)
    agent.run("what is this?")
    assert agent._turn is None and (tmp_path / "x.py").exists()
