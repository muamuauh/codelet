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
def client(monkeypatch, tmp_path):
    from codelet.llm.models import ModelCatalog
    monkeypatch.setattr("codelet.agent_loop.build_client", lambda config: ScriptedClient())
    monkeypatch.setattr("codelet.plugins.builtin.cockpit.default_usage_path", lambda: tmp_path / "cockpit_usage.jsonl")
    monkeypatch.setattr("codelet.web.server._workspace_states_file", lambda: tmp_path / "workspace_states.json")
    return TestClient(create_app(
        catalog=ModelCatalog(cache_path=tmp_path / "models_cache.json"),
        auto_refresh=False,
    ))


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
    names = [e["name"] for e in r["entries"]]
    assert "sub1" in names and "sub2" in names
    assert ".hidden" not in names
    # entries carry full paths (so drives/other roots are navigable)
    assert all(e["path"].endswith(e["name"]) for e in r["entries"])


def test_browse_defaults_to_drive_list(client):
    """The picker opens at the very top (drive roots), not the home directory."""
    r = client.get("/api/browse").json()
    assert r["is_root"] is True and r["parent"] is None
    assert r["entries"]                              # C:\, D:\ ... (POSIX: /)
    assert all(e["name"] == e["path"] for e in r["entries"])
    # explicitly-empty path behaves the same as no path at all
    assert client.get("/api/browse", params={"path": ""}).json() == r


def test_browse_drives_root(client):
    r = client.get("/api/browse", params={"path": "::drives::"}).json()
    assert r["is_root"] is True and r["parent"] is None
    assert r["entries"]  # at least one drive/root
    assert all(e["name"] == e["path"] for e in r["entries"])


def test_ws_set_model(client):
    with client.websocket_connect("/ws") as ws:
        _recv_until(ws, "profile")
        ws.send_json({"type": "set_model", "model": "foo/bar-42"})
        f = _recv_until(ws, "profile")
        assert f["model"] == "foo/bar-42"


def test_ws_new_conversation_clears(client):
    with client.websocket_connect("/ws") as ws:
        _recv_until(ws, "cockpit", limit=12)  # drain initial connect frames
        ws.send_json({"type": "new_conversation"})
        kinds = set()
        for _ in range(10):
            kinds.add(ws.receive_json()["type"])
            if {"cleared", "profile", "workspace"}.issubset(kinds):
                break
        assert {"cleared", "profile", "workspace"}.issubset(kinds)


def test_ws_tool_toggle(client):
    with client.websocket_connect("/ws") as ws:
        tools = _recv_until(ws, "tools")["tools"]
        assert tools and all("enabled" in t for t in tools)
        name = tools[0]["name"]
        ws.send_json({"type": "set_tool_enabled", "name": name, "enabled": False})
        updated = _recv_until(ws, "tools")["tools"]
        assert any(t["name"] == name and t["enabled"] is False for t in updated)


def test_rename_and_delete_session(tmp_path):
    import json as _json
    from codelet.persistence.session import (
        delete_session, list_project_sessions, record_project_session, rename_session,
    )
    base = tmp_path / "sessions"; base.mkdir()
    proj = tmp_path / ".codelet"; proj.mkdir()
    sid = "20260101-000000-ab"
    (base / f"{sid}.json").write_text(
        _json.dumps({"id": sid, "summary": "old title", "messages": []}), encoding="utf-8")
    record_project_session(proj, session_id=sid, title="old title", updated_at="t", message_count=0)

    rename_session(sid, "new title", base_dir=base, project_dir=proj)
    assert list_project_sessions(proj)[0]["title"] == "new title"
    assert _json.loads((base / f"{sid}.json").read_text())["summary"] == "new title"

    delete_session(sid, base_dir=base, project_dir=proj)
    assert not (base / f"{sid}.json").exists()
    assert list_project_sessions(proj) == []


def test_upload_saves_files(client, tmp_path):
    os.chdir(tmp_path)  # uploads land under cwd/.codelet/uploads; restored by fixture
    r = client.post("/api/upload", files=[
        ("files", ("a.txt", b"hello", "text/plain")),
        ("files", ("pic.png", b"\x89PNG\r\n\x1a\n", "image/png")),
    ])
    assert r.status_code == 200
    saved = r.json()["files"]
    assert {f["name"] for f in saved} == {"a.txt", "pic.png"}
    assert any(f["name"] == "pic.png" and f["is_image"] for f in saved)
    assert any(f["name"] == "a.txt" and not f["is_image"] for f in saved)
    for f in saved:
        assert os.path.isfile(f["path"])


def test_ws_set_workspace(client, tmp_path):
    (tmp_path / "proj").mkdir()
    target = str((tmp_path / "proj").resolve())
    with client.websocket_connect("/ws") as ws:
        _recv_until(ws, "workspace")
        ws.send_json({"type": "set_workspace", "path": target})
        f = _recv_until(ws, "workspace")
        assert f["cwd"] == target
        assert os.path.samefile(os.getcwd(), target)


def test_api_plugins_and_ws_plugin_toggle(client):
    plugins = client.get("/api/plugins").json()
    names = {p["name"] for p in plugins}
    assert {"sandbox", "rag", "evolve", "memory", "router"}.issubset(names)

    with client.websocket_connect("/ws") as ws:
        tools = _recv_until(ws, "tools")["tools"]
        assert any(t["name"] == "read_file" and t["source"] == "core" for t in tools)
        plug_frame = _recv_until(ws, "plugins")
        cmd_frame = _recv_until(ws, "commands")
        assert isinstance(cmd_frame["commands"], list)
        assert any(p["name"] == "router" for p in plug_frame["plugins"])

        # Enable `memory` plugin live via WS and verify its tool appears with source="memory".
        ws.send_json({"type": "set_plugin_enabled", "name": "memory", "enabled": True})
        p_after = _recv_until(ws, "plugins")["plugins"]
        t_after = _recv_until(ws, "tools")["tools"]
        assert any(p["name"] == "memory" and p["enabled"] for p in p_after)
        assert any(t["name"] == "memory" and t["source"] == "memory" for t in t_after)

        # Disable it again and verify the tool disappears cleanly.
        ws.send_json({"type": "set_plugin_enabled", "name": "memory", "enabled": False})
        p_off = _recv_until(ws, "plugins")["plugins"]
        t_off = _recv_until(ws, "tools")["tools"]
        assert any(p["name"] == "memory" and not p["enabled"] for p in p_off)
        assert not any(t["name"] == "memory" for t in t_off)


def test_first_prompt_persists_immediately_generates_title_and_notifies_sidebar(monkeypatch, tmp_path):
    from codelet.llm.models import ModelCatalog

    class _TitleAwareClient(LLMClient):
        def chat(self, *, messages: Any, system: str, tools: Any, model: str, max_tokens: int = 8192, on_text: Any = None, **kwargs: Any) -> LLMResponse:
            if "Generate a concise conversation title" in system:
                return LLMResponse(
                    text_blocks=['"项目背景与架构梳理。"'],
                    raw_content=[{"type": "text", "text": '"项目背景与架构梳理。"'}],
                    stop_reason="end_turn",
                )
            return LLMResponse(
                text_blocks=["Main reply"],
                raw_content=[{"type": "text", "text": "Main reply"}],
                stop_reason="end_turn",
                usage={"input_tokens": 10, "output_tokens": 5},
            )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codelet.agent_loop.build_client", lambda config: _TitleAwareClient())
    monkeypatch.setattr("codelet.plugins.builtin.cockpit.default_usage_path", lambda: tmp_path / "cockpit_usage.jsonl")
    monkeypatch.setattr("codelet.web.server._workspace_states_file", lambda: tmp_path / "workspace_states.json")
    tc = TestClient(create_app(
        catalog=ModelCatalog(cache_path=tmp_path / "models_cache.json"),
        auto_refresh=False,
    ))

    with tc.websocket_connect("/ws") as ws:
        _recv_until(ws, "cockpit", limit=12)
        ws.send_json({"type": "prompt", "text": "看一下这个项目，阅读一下相关文档和代码"})
        frames = []
        for _ in range(40):
            f = ws.receive_json()
            frames.append(f)
            if f["type"] == "turn_done":
                break
        # At least one sessions_changed with active_session_id must have been emitted
        sc_frames = [f for f in frames if f["type"] == "sessions_changed" and f.get("active_session_id")]
        assert sc_frames
        sid = sc_frames[0]["active_session_id"]

        # Drain a moment if the background title task sends its update right after turn_done
        import time
        deadline = time.time() + 3.0
        title = ""
        while time.time() < deadline:
            data = tc.get("/api/sessions").json()
            rows = data.get("project") or []
            hit = next((r for r in rows if r["id"] == sid), None)
            if hit and hit.get("title") == "项目背景与架构梳理":
                title = hit["title"]
                break
            time.sleep(0.05)
        assert title == "项目背景与架构梳理"

        # A second turn must NOT clobber the generated title back to the raw first prompt!
        ws.send_json({"type": "prompt", "text": "继续分析测试目录"})
        for _ in range(40):
            if ws.receive_json()["type"] == "turn_done":
                break
        rows_after = tc.get("/api/sessions").json().get("project") or []
        hit_after = next(r for r in rows_after if r["id"] == sid)
        assert hit_after["title"] == "项目背景与架构梳理"

