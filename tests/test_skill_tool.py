"""Skill tool tests."""
from __future__ import annotations

from codelet.skills.loader import Skill, SkillIndex
from codelet.tools.skill_tool import SkillTool


def _index_with(*pairs: tuple[str, str, str]) -> SkillIndex:
    idx = SkillIndex()
    for name, desc, body in pairs:
        idx.add(Skill(name=name, description=desc, body=body))
    return idx


def test_skill_fetch_returns_body():
    idx = _index_with(("review", "do a review", "Step 1: read.\nStep 2: assess."))
    tool = SkillTool(idx)
    result = tool.execute({"name": "review"})
    assert not result.is_error
    assert "Step 1" in result.output


def test_skill_fetch_unknown_lists_available():
    idx = _index_with(("a", "ad", "ab"), ("b", "bd", "bb"))
    tool = SkillTool(idx)
    result = tool.execute({"name": "missing"})
    assert result.is_error
    assert "a" in result.output and "b" in result.output


def test_skill_fetch_empty_name():
    tool = SkillTool(SkillIndex())
    assert tool.execute({"name": ""}).is_error


def test_agent_loop_auto_registers_skill_when_present():
    """Skill tool should appear in the registry only when the skill index
    has at least one entry (otherwise we'd advertise an empty capability)."""
    from codelet.agent_loop import AgentLoop
    from codelet.config import Config, PermissionMode
    from codelet.llm.base import LLMClient, LLMResponse
    from codelet.tools.base import ToolRegistry

    class StubClient(LLMClient):
        def chat(self, **kwargs):
            return LLMResponse()

    cfg = Config(permission_mode=PermissionMode.AUTO)

    # Empty index -> no skill tool.
    empty_loop = AgentLoop(
        config=cfg, registry=ToolRegistry(), client=StubClient(), skill_index=SkillIndex()
    )
    assert "skill" not in empty_loop.registry.names()

    # Populated index -> skill tool registered.
    idx = _index_with(("review", "do a review", "Step 1: read."))
    populated_loop = AgentLoop(
        config=cfg, registry=ToolRegistry(), client=StubClient(), skill_index=idx
    )
    assert "skill" in populated_loop.registry.names()
