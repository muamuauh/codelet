"""Interactive GUI flows over the real WebSocket protocol the browser speaks:
ASK-mode diff approval (Approve runs the write, Reject blocks it) and a runtime
ASK->AUTO switch (writes then run with no prompt). A scripted model makes the
tool calls deterministic; a temp cwd keeps the writes out of the repo.
"""
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import codelet.agent_loop as agent_loop  # noqa: E402
from codelet.llm.base import LLMClient, LLMResponse, ToolCall  # noqa: E402


def _tool_use(i: int, path: str, content: str) -> LLMResponse:
    tc = ToolCall(id=f"w{i}", name="write_file", input={"path": path, "content": content})
    return LLMResponse(tool_calls=[tc], stop_reason="tool_use",
                       raw_content=[{"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}])


def _text(t: str) -> LLMResponse:
    return LLMResponse(text_blocks=[t], stop_reason="end_turn",
                       raw_content=[{"type": "text", "text": t}],
                       usage={"input_tokens": 2, "output_tokens": 2})


class _Stub(LLMClient):
    def __init__(self, script: list[LLMResponse]) -> None:
        self.script, self.i = list(script), 0

    def chat(self, *, on_text: Any = None, **k: Any) -> LLMResponse:
        r = self.script[self.i]
        self.i += 1
        if on_text is not None:
            for t in r.text_blocks:
                on_text(t)
        return r


def _client(monkeypatch, tmp_path, script: list[LLMResponse]) -> TestClient:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_loop, "build_client", lambda config: _Stub(script))
    from codelet.web.server import create_app
    return TestClient(create_app())


def _drain(ws, on_permission=None) -> list[dict]:
    """Read frames until turn_done; call on_permission(frame) for approval prompts."""
    frames = []
    while True:
        f = ws.receive_json()
        frames.append(f)
        if f["type"] == "permission_request" and on_permission is not None:
            on_permission(f)
        if f["type"] == "turn_done":
            return frames


def test_ask_approve_runs_the_write(monkeypatch, tmp_path):
    target = str(tmp_path / "approved.txt")
    client = _client(monkeypatch, tmp_path,
                     [_tool_use(1, target, "yes"), _text("wrote it")])
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "profile"
        ws.send_json({"type": "prompt", "text": "write it"})
        frames = _drain(ws, on_permission=lambda f: ws.send_json({"type": "approve", "id": f["id"]}))
    kinds = [f["type"] for f in frames]
    assert "permission_request" in kinds
    assert any(f["type"] == "tool_result" and not f["is_error"] for f in frames)
    assert (tmp_path / "approved.txt").read_text() == "yes"


def test_ask_reject_blocks_the_write(monkeypatch, tmp_path):
    target = str(tmp_path / "rejected.txt")
    client = _client(monkeypatch, tmp_path,
                     [_tool_use(2, target, "no"), _text("understood")])
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "profile"
        ws.send_json({"type": "prompt", "text": "write it"})
        frames = _drain(ws, on_permission=lambda f: ws.send_json({"type": "reject", "id": f["id"]}))
    assert any(f["type"] == "permission_request" for f in frames)
    assert any(f["type"] == "notice" and "rejected" in f["message"] for f in frames)
    assert not any(f["type"] == "tool_result" and not f["is_error"] for f in frames)
    assert not (tmp_path / "rejected.txt").exists()


def _create_tool_call(i: int, name: str, code: str) -> LLMResponse:
    inp = {"name": name, "description": f"{name} tool",
           "parameters": {"properties": {"x": {"type": "string"}}}, "code": code}
    tc = ToolCall(id=f"c{i}", name="create_tool", input=inp)
    return LLMResponse(tool_calls=[tc], stop_reason="tool_use",
                       raw_content=[{"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}])


def test_create_tool_pushes_tools_frame(monkeypatch, tmp_path):
    """Self-evolution: after a turn that registers a new tool, the server pushes a
    fresh `tools` frame so the sidebar refreshes without a reconnect."""
    import json
    d = tmp_path / ".codelet"; d.mkdir()
    (d / "settings.json").write_text(json.dumps({
        "plugins": {"enabled": ["evolve"],
                    "config": {"evolve": {"dir": str(tmp_path / "evolved")}}},
    }))
    client = _client(monkeypatch, tmp_path, [
        _create_tool_call(1, "shout", "return params['x'].upper()"),
        _text("made a tool"),
    ])
    with client.websocket_connect("/ws") as ws:
        for _ in range(6):                       # drain connect frames up to `skills`
            if ws.receive_json()["type"] == "skills":
                break
        ws.send_json({"type": "set_mode", "mode": "auto"})   # skip the ASK preview
        for _ in range(4):
            if ws.receive_json()["type"] == "profile":
                break
        ws.send_json({"type": "prompt", "text": "make a shout tool"})
        frames = _drain(ws)
    tool_frames = [f for f in frames if f["type"] == "tools"]
    assert tool_frames, "expected a tools frame after the registry changed"
    names = [t["name"] for t in tool_frames[-1]["tools"]]
    assert "shout" in names                      # the newly authored tool shows up


def test_no_tools_frame_when_registry_unchanged(monkeypatch, tmp_path):
    """A normal turn that doesn't touch the tool set sends no `tools` frame."""
    target = str(tmp_path / "x.txt")
    client = _client(monkeypatch, tmp_path,
                     [_tool_use(9, target, "hi"), _text("done")])
    with client.websocket_connect("/ws") as ws:
        for _ in range(6):
            if ws.receive_json()["type"] == "skills":
                break
        ws.send_json({"type": "set_mode", "mode": "auto"})
        for _ in range(4):
            if ws.receive_json()["type"] == "profile":
                break
        ws.send_json({"type": "prompt", "text": "write it"})
        frames = _drain(ws)
    assert not any(f["type"] == "tools" for f in frames)


def test_auto_mode_switch_skips_approval(monkeypatch, tmp_path):
    target = str(tmp_path / "auto.txt")
    client = _client(monkeypatch, tmp_path,
                     [_tool_use(3, target, "auto"), _text("done")])
    with client.websocket_connect("/ws") as ws:
        # drain connect frames (profile, workspace, tools, skills)
        for _ in range(6):
            if ws.receive_json()["type"] == "skills":
                break
        ws.send_json({"type": "set_mode", "mode": "auto"})
        prof = None
        for _ in range(6):
            f = ws.receive_json()
            if f["type"] == "profile":
                prof = f
                break
        assert prof is not None and prof["mode"] == "auto"
        ws.send_json({"type": "prompt", "text": "write it"})
        frames = _drain(ws)  # no approval handler -> there must be no request
    assert not any(f["type"] == "permission_request" for f in frames)
    assert any(f["type"] == "tool_result" and not f["is_error"] for f in frames)
    assert (tmp_path / "auto.txt").read_text() == "auto"
