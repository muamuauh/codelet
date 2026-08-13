"""FileWrite / FileEdit diff preview + ASK confirm tests (P5)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codelet.agent_loop import AgentLoop
from codelet.config import Config, PermissionMode
from codelet.llm.base import LLMClient, LLMResponse, ToolCall
from codelet.tools.base import ToolRegistry
from codelet.tools.file_edit import FileEditTool
from codelet.tools.file_write import FileWriteTool


# ---------- preview_diff direct ----------

def test_filewrite_preview_diff_for_new_file(tmp_path: Path):
    target = tmp_path / "new.txt"
    diff = FileWriteTool().preview_diff({"path": str(target), "content": "hello\n"})
    assert diff is not None
    assert "(new file)" in diff
    assert "+hello" in diff


def test_filewrite_preview_diff_for_existing_file(tmp_path: Path):
    target = tmp_path / "existing.txt"
    target.write_text("alpha\nbeta\n")
    diff = FileWriteTool().preview_diff({"path": str(target), "content": "alpha\nGAMMA\n"})
    assert diff is not None
    assert "-beta" in diff
    assert "+GAMMA" in diff


def test_filewrite_preview_identical_content_says_no_diff(tmp_path: Path):
    target = tmp_path / "same.txt"
    target.write_text("x\n")
    diff = FileWriteTool().preview_diff({"path": str(target), "content": "x\n"})
    assert "no diff" in diff


def test_fileedit_preview_diff_uniquely_matched(tmp_path: Path):
    target = tmp_path / "code.py"
    target.write_text("a = 1\nb = 2\nc = 3\n")
    diff = FileEditTool().preview_diff({
        "path": str(target),
        "old_string": "b = 2",
        "new_string": "b = 22",
    })
    assert "-b = 2" in diff
    assert "+b = 22" in diff


def test_fileedit_preview_diff_no_unique_match(tmp_path: Path):
    target = tmp_path / "dup.txt"
    target.write_text("foo\nfoo\n")
    diff = FileEditTool().preview_diff({
        "path": str(target), "old_string": "foo", "new_string": "bar",
    })
    assert "not uniquely matched" in diff


def test_fileedit_preview_diff_missing_file(tmp_path: Path):
    diff = FileEditTool().preview_diff({
        "path": str(tmp_path / "nope"), "old_string": "x", "new_string": "y",
    })
    assert "not found" in diff


# ---------- agent_loop integration ----------

class StubLLM(LLMClient):
    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)

    def chat(self, **kwargs: Any) -> LLMResponse:
        if not self.script:
            return LLMResponse(text_blocks=["(end)"],
                               raw_content=[{"type": "text", "text": "(end)"}],
                               stop_reason="end_turn")
        return self.script.pop(0)


def _tool_resp(call: ToolCall) -> LLMResponse:
    return LLMResponse(
        tool_calls=[call],
        raw_content=[{"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}],
        stop_reason="tool_use",
    )


def _text_resp(text: str) -> LLMResponse:
    return LLMResponse(text_blocks=[text],
                       raw_content=[{"type": "text", "text": text}],
                       stop_reason="end_turn")


@pytest.mark.asyncio
async def test_ask_mode_rejection_blocks_write(tmp_path: Path):
    """User says 'n' -> file untouched, error result returned to LLM."""
    target = tmp_path / "blocked.txt"
    cfg = Config(permission_mode=PermissionMode.ASK)
    reg = ToolRegistry()
    reg.register(FileWriteTool())

    captured_diff: list[str] = []
    def deny(tool_name: str, diff: str) -> bool:
        captured_diff.append(diff)
        return False

    client = StubLLM([
        _tool_resp(ToolCall(id="t1", name="write_file",
                            input={"path": str(target), "content": "should-not-write"})),
        _text_resp("aborted"),
    ])
    agent = AgentLoop(config=cfg, registry=reg, client=client, confirm_callback=deny)
    out = await agent.run_async("go")
    assert out == "aborted"
    assert not target.exists()
    # The confirm callback was called with a diff we can verify.
    assert captured_diff and "should-not-write" in captured_diff[0]
    # The result fed back to the model is is_error=True with the rejection text.
    last_user = [m for m in agent.context.messages if m["role"] == "user"][-1]
    block = last_user["content"][0]
    assert block["is_error"] is True
    assert "rejected" in block["content"].lower()


@pytest.mark.asyncio
async def test_ask_mode_acceptance_writes_file(tmp_path: Path):
    target = tmp_path / "accepted.txt"
    cfg = Config(permission_mode=PermissionMode.ASK)
    reg = ToolRegistry()
    reg.register(FileWriteTool())

    client = StubLLM([
        _tool_resp(ToolCall(id="t1", name="write_file",
                            input={"path": str(target), "content": "content!"})),
        _text_resp("ok"),
    ])
    agent = AgentLoop(config=cfg, registry=reg, client=client,
                      confirm_callback=lambda *_: True)
    await agent.run_async("go")
    assert target.read_text() == "content!"


@pytest.mark.asyncio
async def test_auto_mode_skips_confirmation(tmp_path: Path):
    """In AUTO mode, confirm_callback must NOT be called even if provided."""
    target = tmp_path / "auto.txt"
    cfg = Config(permission_mode=PermissionMode.AUTO)
    reg = ToolRegistry()
    reg.register(FileWriteTool())

    confirm_calls: list[Any] = []
    def reject_all(*args, **kwargs):
        confirm_calls.append(args)
        return False

    client = StubLLM([
        _tool_resp(ToolCall(id="t1", name="write_file",
                            input={"path": str(target), "content": "auto-content"})),
        _text_resp("ok"),
    ])
    agent = AgentLoop(config=cfg, registry=reg, client=client, confirm_callback=reject_all)
    await agent.run_async("go")
    # Despite reject_all, AUTO mode skips the confirm step entirely.
    assert confirm_calls == []
    assert target.read_text() == "auto-content"
