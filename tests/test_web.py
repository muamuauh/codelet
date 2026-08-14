"""Web backend: REST panels + a WebSocket prompt round-trip, with a stub LLM.

The real LLM client is swapped for a scripted one (patching the name AgentLoop
binds, `codelet.agent_loop.build_client`), so a prompt drives the loop end to end
without any network, and we assert the browser-facing frames come back.
"""
from __future__ import annotations

import os
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from codelet.llm.base import LLMClient, LLMResponse  # noqa: E402
from codelet.web.server import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_cwd():
    # set_workspace chdir's the process; keep tests isolated from each other.
    cwd = os.getcwd()
    yield
    os.chdir(cwd)


def _recv_until(ws, kind: str, limit: int = 10) -> dict:
    for _ in range(limit):
        f = ws.receive_json()
        if f["type"] == kind:
            return f
    raise AssertionError(f"no {kind} frame within {limit}")


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
        assert ws.receive_json()["type"] == "workspace"  # sent on connect
        ws.send_json({"type": "set_mode", "mode": "auto"})
        f = _recv_until(ws, "profile")
        assert f["mode"] == "auto"


def test_profile_lists_env_models(client, monkeypatch):
    monkeypatch.setenv("LLM_MODELS", "m1/a, m2/b ,m3/c")
    models = client.get("/api/profile").json()["models"]
    assert {"m1/a", "m2/b", "m3/c"}.issubset(set(models))


def test_browse_lists_visible_dirs(client, tmp_path):
    (tmp_path / "sub1").mkdir()
    (tmp_path / "sub2").mkdir()
    (tmp_path / ".hidden").mkdir()
    r = client.get("/api/browse", params={"path": str(tmp_path)}).json()
    assert r["path"] == str(tmp_path.resolve())
    assert "sub1" in r["dirs"] and "sub2" in r["dirs"]
    assert ".hidden" not in r["dirs"]


def test_ws_set_model(client):
    with client.websocket_connect("/ws") as ws:
        _recv_until(ws, "profile")
        ws.send_json({"type": "set_model", "model": "foo/bar-42"})
        f = _recv_until(ws, "profile")
        assert f["model"] == "foo/bar-42"


def test_ws_new_conversation_clears(client):
    with client.websocket_connect("/ws") as ws:
        _recv_until(ws, "workspace")
        ws.send_json({"type": "new_conversation"})
        kinds = {ws.receive_json()["type"] for _ in range(3)}
        assert {"cleared", "profile", "workspace"}.issubset(kinds)


def test_ws_set_workspace(client, tmp_path):
    (tmp_path / "proj").mkdir()
    target = str((tmp_path / "proj").resolve())
    with client.websocket_connect("/ws") as ws:
        _recv_until(ws, "workspace")
        ws.send_json({"type": "set_workspace", "path": target})
        f = _recv_until(ws, "workspace")
        assert f["cwd"] == target
        assert os.path.samefile(os.getcwd(), target)
