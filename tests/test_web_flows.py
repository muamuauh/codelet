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


def test_auto_mode_switch_skips_approval(monkeypatch, tmp_path):
    target = str(tmp_path / "auto.txt")
    client = _client(monkeypatch, tmp_path,
                     [_tool_use(3, target, "auto"), _text("done")])
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "profile"
        assert ws.receive_json()["type"] == "workspace"  # sent on connect
        ws.send_json({"type": "set_mode", "mode": "auto"})
        prof = ws.receive_json()
        assert prof["type"] == "profile" and prof["mode"] == "auto"
        ws.send_json({"type": "prompt", "text": "write it"})
        frames = _drain(ws)  # no approval handler -> there must be no request
    assert not any(f["type"] == "permission_request" for f in frames)
    assert any(f["type"] == "tool_result" and not f["is_error"] for f in frames)
    assert (tmp_path / "auto.txt").read_text() == "auto"
