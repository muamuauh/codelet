"""Web backend: REST panels + a WebSocket prompt round-trip, with a stub LLM.

The real LLM client is swapped for a scripted one (patching the name AgentLoop
binds, `codelet.agent_loop.build_client`), so a prompt drives the loop end to end
without any network, and we assert the browser-facing frames come back.
"""
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from codelet.llm.base import LLMClient, LLMResponse  # noqa: E402
from codelet.web.server import create_app  # noqa: E402


class ScriptedClient(LLMClient):
    def chat(self, *, on_text: Any = None, **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            text_blocks=["hi from stub"],
            raw_content=[{"type": "text", "text": "hi from stub"}],
            stop_reason="end_turn", usage={"input_tokens": 3, "output_tokens": 4},
        )


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("codelet.agent_loop.build_client", lambda config: ScriptedClient())
    return TestClient(create_app())


def test_rest_panels(client):
    tools = client.get("/api/tools").json()
    assert isinstance(tools, list) and any(t.get("name") for t in tools)
    prof = client.get("/api/profile").json()
    assert "provider" in prof and "mode" in prof
    sess = client.get("/api/sessions").json()
    assert "project" in sess and "global" in sess
    # /api/skills and /api/commands must return 200 with string fields even when
    # a skill's `source` is a Path (a live run caught that as a 500).
    r = client.get("/api/skills")
    assert r.status_code == 200
    assert all(isinstance(s.get("source"), str) for s in r.json())
    assert client.get("/api/commands").status_code == 200


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "codelet" in r.text.lower()


def test_ws_prompt_streams_back(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "profile"
        ws.send_json({"type": "prompt", "text": "hello"})
        frames = []
        for _ in range(40):
            f = ws.receive_json()
            frames.append(f)
            if f["type"] == "turn_done":
                break
        types = [f["type"] for f in frames]
        assert "text_delta" in types
        assert "turn_done" in types
        assert any(f["type"] == "text_delta" and "hi from stub" in f.get("text", "") for f in frames)
        assert any(f["type"] == "telemetry" for f in frames)


def test_set_mode_echoes_profile(client):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "profile"
        ws.send_json({"type": "set_mode", "mode": "auto"})
        f = ws.receive_json()
        assert f["type"] == "profile" and f["mode"] == "auto"
