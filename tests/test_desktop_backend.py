"""Tests for codelet Desktop backend extensions (health, token auth, shutdown)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from codelet.web.server import DESKTOP_COOKIE, create_app


def test_health_endpoint_browser_mode():
    app = create_app(auto_refresh=False)
    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["desktop"] is False
    assert isinstance(body["pid"], int)
    assert body["cwd"]


def test_browser_mode_has_no_token_restriction():
    app = create_app(auto_refresh=False)
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/api/profile").status_code == 200


def test_desktop_token_protects_api_and_index_but_allows_health():
    tok = "secret-desktop-token-123"
    app = create_app(auto_refresh=False, desktop_token=tok)
    client = TestClient(app)

    # /api/health is always accessible for Electron readiness polling
    h = client.get("/api/health")
    assert h.status_code == 200
    assert h.json()["desktop"] is True

    # Without token: index and API endpoints return 403
    assert client.get("/").status_code == 403
    assert client.get("/api/profile").status_code == 403
    assert client.get("/api/sessions").status_code == 403
    assert client.post("/api/shutdown").status_code == 403

    # Wrong token is also rejected
    assert client.get("/api/profile", headers={"X-Codelet-Token": "wrong"}).status_code == 403

    # Valid header unlocks API
    ok_hdr = client.get("/api/profile", headers={"X-Codelet-Token": tok})
    assert ok_hdr.status_code == 200

    # Entry URL /?desktop_token=<tok> sets HttpOnly cookie and redirects to /
    entry = client.get(f"/?desktop_token={tok}", follow_redirects=False)
    assert entry.status_code == 302
    assert entry.headers["location"] == "/"
    assert entry.cookies.get(DESKTOP_COOKIE) == tok

    # Subsequent requests using the cookie succeed without repeating the query param
    assert client.get("/").status_code == 200
    assert client.get("/api/profile").status_code == 200


def test_desktop_token_protects_websocket():
    tok = "ws-secret-token-456"
    app = create_app(auto_refresh=False, desktop_token=tok)
    client = TestClient(app)

    # Rejects unauthenticated websocket
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws"):
            pass
    assert exc.value.code == 1008

    # Accepts authenticated websocket via header or cookie
    with client.websocket_connect("/ws", headers={"X-Codelet-Token": tok}) as ws:
        frame = ws.receive_json()
        assert frame["type"] == "profile"


def test_shutdown_disposes_active_plugins(monkeypatch):
    from codelet.plugins.loader import AppliedPlugins

    tok = "shutdown-tok"
    app = create_app(auto_refresh=False, desktop_token=tok)
    client = TestClient(app)

    exited = []
    monkeypatch.setattr("os._exit", lambda code: exited.append(code))
    monkeypatch.setattr("time.sleep", lambda _s: None)

    class _SyncThread:
        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    monkeypatch.setattr("threading.Thread", _SyncThread)

    dispose_calls = []
    orig_dispose = AppliedPlugins.dispose_all

    def _spy_dispose(self, registry=None):
        dispose_calls.append(len(self.handles))
        return orig_dispose(self, registry)

    monkeypatch.setattr(AppliedPlugins, "dispose_all", _spy_dispose)

    with client.websocket_connect("/ws", headers={"X-Codelet-Token": tok}) as ws:
        ws.receive_json()
        r = client.post("/api/shutdown", headers={"X-Codelet-Token": tok})
        assert r.status_code == 200
        assert r.json() == {"status": "shutting down"}

    assert len(dispose_calls) >= 1
    assert exited == [0]


def test_settings_apply_masks_secret_and_never_writes_plaintext_key(tmp_path, monkeypatch):
    import json
    import os

    fake_settings = tmp_path / "settings.json"
    monkeypatch.setattr("codelet.web.server._user_settings_file", lambda: fake_settings)
    monkeypatch.setattr("codelet.web.server._workspace_states_file", lambda: tmp_path / "workspace_states.json")
    monkeypatch.delenv("CODELET_TEST_SECRET_KEY", raising=False)

    tok = "settings-tok-789"
    app = create_app(auto_refresh=False, desktop_token=tok)
    client = TestClient(app)

    # Guarded by desktop token
    assert client.get("/api/settings").status_code == 403
    assert client.post("/api/settings/apply", json={}).status_code == 403

    hdrs = {"X-Codelet-Token": tok}
    res = client.post(
        "/api/settings/apply",
        headers=hdrs,
        json={
            "profile": "myrelay",
            "provider": "openai",
            "base_url": "https://example.invalid/v1",
            "model": "gpt-4.1-mini",
            "api_key_env": "CODELET_TEST_SECRET_KEY",
            "api_key": "sk-super-secret-value-9876",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["active_profile"] == "myrelay"
    assert body["api_key_configured"] is True
    # Raw key must NEVER appear in API responses or on disk
    assert "sk-super-secret-value-9876" not in json.dumps(body)
    assert body["api_key_preview"] == "sk-••••9876"

    saved_text = fake_settings.read_text(encoding="utf-8")
    assert "sk-super-secret-value-9876" not in saved_text
    saved = json.loads(saved_text)
    assert saved["profile"] == "myrelay"
    assert saved["profiles"]["myrelay"]["base_url"] == "https://example.invalid/v1"
    assert saved["profiles"]["myrelay"]["api_key_env"] == "CODELET_TEST_SECRET_KEY"
    assert "api_key" not in saved["profiles"]["myrelay"]
    assert os.environ.get("CODELET_TEST_SECRET_KEY") == "sk-super-secret-value-9876"

    # Clear key removes in-memory env var
    clr = client.post(
        "/api/settings/apply",
        headers=hdrs,
        json={
            "profile": "myrelay",
            "provider": "openai",
            "base_url": "https://example.invalid/v1",
            "model": "gpt-4.1-mini",
            "api_key_env": "CODELET_TEST_SECRET_KEY",
            "clear_api_key": True,
        },
    )
    assert clr.status_code == 200
    assert "CODELET_TEST_SECRET_KEY" not in os.environ
    assert clr.json()["profiles"]["myrelay"]["api_key_configured"] is False

    # Delete profile
    dl = client.post("/api/settings/apply", headers=hdrs, json={"delete_profile": "myrelay"})
    assert dl.status_code == 200
    assert "myrelay" not in json.loads(fake_settings.read_text(encoding="utf-8")).get("profiles", {})


def test_settings_test_endpoint_missing_key():
    app = create_app(auto_refresh=False)
    client = TestClient(app)
    r = client.post(
        "/api/settings/test",
        json={
            "provider": "openai",
            "base_url": "https://example.invalid/v1",
            "api_key_env": "NON_EXISTENT_TEST_KEY_XYZ",
            "api_key": "",
        },
    )
    assert r.status_code == 200
    # Either reports missing key or uses active fallback; must never 500
    assert "ok" in r.json()


def test_profile_custom_model_directory_and_display_names(tmp_path, monkeypatch):
    import json

    fake_settings = tmp_path / "settings.json"
    monkeypatch.setattr("codelet.web.server._user_settings_file", lambda: fake_settings)
    monkeypatch.setattr("codelet.web.server._workspace_states_file", lambda: tmp_path / "workspace_states.json")
    monkeypatch.setenv("CODELET_DIR_KEY", "sk-dir-test-1234")

    app = create_app(auto_refresh=False)
    client = TestClient(app)

    res = client.post(
        "/api/settings/apply",
        json={
            "profile": "relay1",
            "provider": "openai",
            "base_url": "https://relay.example.com/v1",
            "model": "deepseek-flash",
            "api_key_env": "CODELET_DIR_KEY",
            "models": [
                {"id": "deepseek-flash", "display_name": "DeepSeek-V4.1-Flash"},
                {"id": "deepseek-v4-pro", "display_name": "DeepSeek-V4-Pro"},
            ],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["profiles"]["relay1"]["models"] == [
        {"id": "deepseek-flash", "display_name": "DeepSeek-V4.1-Flash"},
        {"id": "deepseek-v4-pro", "display_name": "DeepSeek-V4-Pro"},
    ]

    # /api/profile reflects custom display names & ordering
    prof = client.get("/api/profile").json()
    assert prof["models"][:2] == ["deepseek-flash", "deepseek-v4-pro"]
    by_id = {m["id"]: m for m in prof["model_catalog"]}
    assert by_id["deepseek-flash"]["display_name"] == "DeepSeek-V4.1-Flash"
    assert by_id["deepseek-flash"]["custom"] is True
    assert by_id["deepseek-v4-pro"]["display_name"] == "DeepSeek-V4-Pro"

    saved = json.loads(fake_settings.read_text(encoding="utf-8"))
    assert len(saved["profiles"]["relay1"]["models"]) == 2


def test_discover_models_reads_relay_docs_with_configured_llm(monkeypatch):
    from codelet.llm.base import LLMResponse
    from codelet.web.server import _parse_discovered_models, _strip_html_to_text

    # Helper unit checks
    assert "Hello" in _strip_html_to_text("<html><script>bad()</script><body><p>Hello</p></body></html>")
    assert "bad()" not in _strip_html_to_text("<html><script>bad()</script><body><p>Hello</p></body></html>")
    parsed = _parse_discovered_models('```json\n{"models":[{"id":"m1","display_name":"Model One"}]}\n```')
    assert parsed == [{"id": "m1", "display_name": "Model One"}]

    # Mock httpx.AsyncClient to return relay HTML docs + /models list
    class _FakeResp:
        def __init__(self, status_code, text="", json_data=None):
            self.status_code = status_code
            self.text = text
            self._json = json_data

        def json(self):
            return self._json

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            if url.endswith("/v1/models"):
                return _FakeResp(200, '{"data":[{"id":"deepseek-flash"}]}', {"data": [{"id": "deepseek-flash"}]})
            return _FakeResp(
                200,
                "<html><body><h1>Supported Models</h1><p>deepseek-flash (DeepSeek-V4.1-Flash), deepseek-v4-pro (DeepSeek-V4-Pro)</p></body></html>",
            )

    class _FakeLLMClient:
        def chat(self, **kw):
            return LLMResponse(
                text_blocks=[
                    '{"models":[{"id":"deepseek-flash","display_name":"DeepSeek-V4.1-Flash"},{"id":"deepseek-v4-pro","display_name":"DeepSeek-V4-Pro"}]}'
                ]
            )

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr("codelet.web.server.build_client", lambda cfg: _FakeLLMClient())

    app = create_app(auto_refresh=False)
    client = TestClient(app)
    r = client.post(
        "/api/settings/discover-models",
        json={
            "provider": "openai",
            "base_url": "https://my-relay.example/v1",
            "model": "deepseek-flash",
            "api_key": "sk-test-relay-1234",
            "use_llm": True,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["models"] == [
        {"id": "deepseek-flash", "display_name": "DeepSeek-V4.1-Flash"},
        {"id": "deepseek-v4-pro", "display_name": "DeepSeek-V4-Pro"},
    ]
    assert data["source"] in ("docs+api", "docs", "llm+api")


def test_sessions_grouped_by_workspace_folder(tmp_path, monkeypatch):
    import json
    from codelet.persistence.session import list_sessions_by_workspace

    sessions_dir = tmp_path / "global_sessions"
    sessions_dir.mkdir()
    ws_a = tmp_path / "proj_alpha"
    ws_b = tmp_path / "proj_beta"
    ws_a.mkdir()
    ws_b.mkdir()

    (sessions_dir / "s1.json").write_text(json.dumps({
        "id": "s1",
        "workspace": str(ws_a),
        "summary": "Alpha task 1",
        "updated_at": "2026-11-27T10:00:00",
        "messages": [{"role": "user", "content": "hi"}],
    }), encoding="utf-8")
    (sessions_dir / "s2.json").write_text(json.dumps({
        "id": "s2",
        "workspace": str(ws_b),
        "summary": "Beta task 1",
        "updated_at": "2026-11-27T11:00:00",
        "messages": [{"role": "user", "content": "yo"}],
    }), encoding="utf-8")

    groups = list_sessions_by_workspace(base_dir=sessions_dir, current_workspace=ws_a)
    assert len(groups) == 2
    assert groups[0]["name"] == "proj_alpha"
    assert groups[0]["is_current"] is True
    assert [x["id"] for x in groups[0]["sessions"]] == ["s1"]
    assert groups[1]["name"] == "proj_beta"
    assert groups[1]["is_current"] is False
    assert [x["id"] for x in groups[1]["sessions"]] == ["s2"]


def test_cockpit_historical_session_inspection(tmp_path, monkeypatch):
    import json

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "hist-1").with_suffix(".json").write_text(json.dumps({
        "id": "hist-1",
        "workspace": str(tmp_path),
        "summary": "Review parser edge cases",
        "system_prompt": "You are codelet.",
        "permission_mode": "plan",
        "messages": [
            {"role": "user", "content": "Inspect parser.py"},
            {"role": "assistant", "content": "Looks clean!"},
        ],
        "telemetry": {"input_tokens": 1200, "output_tokens": 350, "cost_usd": 0.012, "calls": 2},
    }), encoding="utf-8")

    monkeypatch.setattr("codelet.persistence.session.SESSION_DIR", sessions_dir)
    app = create_app(auto_refresh=False)
    client = TestClient(app)

    r = client.get("/api/cockpit?session_id=hist-1")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["viewing_history"] is True
    assert body["session_id"] == "hist-1"
    assert body["session_title"] == "Review parser edge cases"
    assert body["mode"] == "plan"
    assert body["cache"]["total_calls"] == 2
    assert body["context"]["used"] > 0


def test_per_workspace_persistence_plugins_tools_skills_and_ui_state(tmp_path, monkeypatch):
    ws_a = tmp_path / "ws_alpha"
    ws_b = tmp_path / "ws_beta"
    ws_a.mkdir()
    ws_b.mkdir()

    states_file = tmp_path / "workspace_states.json"
    monkeypatch.setattr("codelet.web.server._workspace_states_file", lambda: states_file)
    monkeypatch.setattr("codelet.web.server._user_settings_file", lambda: tmp_path / "settings.json")
    monkeypatch.chdir(ws_a)

    app = create_app(auto_refresh=False)
    client = TestClient(app)

    # First session in ws_a: enable a plugin, disable a tool & skill, save fold UI state
    with client.websocket_connect("/ws") as ws:
        # Drain initial handshake frames
        init_frames = [ws.receive_json() for _ in range(8)]
        init_tools = next(f for f in init_frames if f["type"] == "tools")["tools"]
        init_skills = next(f for f in init_frames if f["type"] == "skills")["skills"]
        sample_tool = init_tools[0]["name"]
        sample_skill = init_skills[0]["name"] if init_skills else None

        ws.send_json({"type": "set_plugin_enabled", "name": "rag", "enabled": True})
        # Drain emitted frames until cockpit
        while True:
            f = ws.receive_json()
            if f["type"] == "cockpit":
                break

        ws.send_json({"type": "set_tool_enabled", "name": sample_tool, "enabled": False})
        assert ws.receive_json()["type"] == "tools"

        if sample_skill:
            ws.send_json({"type": "set_skill_enabled", "name": sample_skill, "enabled": False})
            assert ws.receive_json()["type"] == "skills"

        ws.send_json({
            "type": "set_ui_state",
            "nav_open": {"nav-sessions": False, "nav-plugins": True},
            "ws_groups_open": {str(ws_a).lower(): False},
        })

    # Reopen (new WS connection in ws_a): must restore plugin, disabled tool/skill, and UI fold state!
    with client.websocket_connect("/ws") as ws:
        frames = [ws.receive_json() for _ in range(8)]
        ws_frame = next(f for f in frames if f["type"] == "workspace")
        plugins_frame = next(f for f in frames if f["type"] == "plugins")["plugins"]
        tools_frame = next(f for f in frames if f["type"] == "tools")["tools"]
        skills_frame = next(f for f in frames if f["type"] == "skills")["skills"]

        assert any(p["name"] == "rag" and p["enabled"] for p in plugins_frame)
        assert any(t["name"] == sample_tool and t["enabled"] is False for t in tools_frame)
        if sample_skill:
            assert any(s["name"] == sample_skill and s["enabled"] is False for s in skills_frame)
        assert ws_frame["ui_state"]["nav_open"]["nav-sessions"] is False
        assert ws_frame["ui_state"]["ws_groups_open"][str(ws_a).lower()] is False

    # Open in ws_b: isolated from ws_a (rag not enabled, sample_tool still enabled, empty ui_state)
    monkeypatch.chdir(ws_b)
    with client.websocket_connect("/ws") as ws:
        frames = [ws.receive_json() for _ in range(8)]
        ws_frame = next(f for f in frames if f["type"] == "workspace")
        plugins_frame = next(f for f in frames if f["type"] == "plugins")["plugins"]
        tools_frame = next(f for f in frames if f["type"] == "tools")["tools"]

        assert any(p["name"] == "rag" and p["enabled"] is False for p in plugins_frame)
        assert any(t["name"] == sample_tool and t["enabled"] is True for t in tools_frame)
        assert ws_frame["ui_state"] == {}


def test_persists_selected_model_and_thinking_effort(tmp_path, monkeypatch):
    ws_a = tmp_path / "ws_model_a"
    ws_b = tmp_path / "ws_model_b"
    ws_a.mkdir()
    ws_b.mkdir()

    states_file = tmp_path / "workspace_states.json"
    monkeypatch.setattr("codelet.web.server._workspace_states_file", lambda: states_file)
    monkeypatch.setattr("codelet.web.server._user_settings_file", lambda: tmp_path / "settings.json")
    monkeypatch.chdir(ws_a)

    app = create_app(auto_refresh=False)
    client = TestClient(app)

    # Pick a specific model & thinking effort in ws_a
    with client.websocket_connect("/ws") as ws:
        for _ in range(8):
            ws.receive_json()
        ws.send_json({"type": "set_model", "model": "anthropic/claude-opus-4.5"})
        p1 = ws.receive_json()
        assert p1["type"] == "profile" and p1["model"] == "anthropic/claude-opus-4.5"
        assert ws.receive_json()["type"] == "cockpit"

        ws.send_json({"type": "set_effort", "effort": "high"})
        p2 = ws.receive_json()
        assert p2["type"] == "profile" and p2["reasoning_effort"] == "high"
        assert ws.receive_json()["type"] == "cockpit"

        ws.send_json({"type": "set_mode", "mode": "auto"})
        p3 = ws.receive_json()
        assert p3["type"] == "profile" and p3["mode"] == "auto"
        assert ws.receive_json()["type"] == "cockpit"

    # Reconnect in ws_a -> must remember model, reasoning_effort, and execution mode
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "profile"
        assert first["model"] == "anthropic/claude-opus-4.5"
        assert first["reasoning_effort"] == "high"
        assert first["mode"] == "auto"

    # Brand new ws_b inherits the last-used defaults, and can override independently
    monkeypatch.chdir(ws_b)
    with client.websocket_connect("/ws") as ws:
        first_b = ws.receive_json()
        assert first_b["type"] == "profile"
        assert first_b["model"] == "anthropic/claude-opus-4.5"
        assert first_b["reasoning_effort"] == "high"
        assert first_b["mode"] == "auto"
        for _ in range(7):
            ws.receive_json()
        ws.send_json({"type": "set_model", "model": "deepseek/deepseek-v4"})
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "set_effort", "effort": "low"})
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "set_mode", "mode": "plan"})
        ws.receive_json()
        ws.receive_json()

    # Returning to ws_a still restores ws_a's own remembered model, effort & mode
    monkeypatch.chdir(ws_a)
    with client.websocket_connect("/ws") as ws:
        first_a2 = ws.receive_json()
        assert first_a2["type"] == "profile"
        assert first_a2["model"] == "anthropic/claude-opus-4.5"
        assert first_a2["reasoning_effort"] == "high"
        assert first_a2["mode"] == "auto"


def test_transcript_groups_multi_step_tools_and_thinking_before_final_answer():
    from codelet.web.server import _transcript

    messages = [
        {"role": "user", "content": "Check repo"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "First read README"},
                {"type": "text", "text": "Let me inspect README first."},
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "README.md"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "# codelet", "is_error": False},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t2", "name": "bash", "input": {"command": "pytest -q"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "354 passed", "is_error": False},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "All checks passed"},
                {"type": "text", "text": "Everything looks healthy!"},
            ],
        },
    ]

    grouped = _transcript(messages)
    assert len(grouped) == 2
    assert grouped[0]["role"] == "user"
    assert grouped[0]["text"] == "Check repo"

    asst = grouped[1]
    assert asst["role"] == "assistant"
    assert asst["text"] == "Everything looks healthy!"
    assert asst["tools"] == ["read_file", "bash"]
    step_types = [s["type"] for s in asst["steps"]]
    assert step_types == ["thinking", "note", "tool", "tool", "thinking"]
    assert asst["steps"][2]["name"] == "read_file"
    assert asst["steps"][2]["output"] == "# codelet"
    assert asst["steps"][3]["name"] == "bash"
    assert asst["steps"][3]["output"] == "354 passed"


def test_cockpit_recognizes_active_resumed_session_as_live(tmp_path, monkeypatch):
    import json

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    sid = "sess-main-live"
    (sessions_dir / f"{sid}.json").write_text(json.dumps({
        "id": sid,
        "workspace": str(tmp_path),
        "summary": "main",
        "system_prompt": "You are codelet.",
        "permission_mode": "ask",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
        "telemetry": {"input_tokens": 500, "output_tokens": 120, "cost_usd": 0.005, "calls": 1},
    }), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codelet.persistence.session.SESSION_DIR", sessions_dir)
    monkeypatch.setattr("codelet.web.server._workspace_states_file", lambda: tmp_path / "workspace_states.json")
    monkeypatch.setattr("codelet.web.server._user_settings_file", lambda: tmp_path / "settings.json")

    app = create_app(auto_refresh=False)
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        # Drain initial handshake frames
        for _ in range(8):
            ws.receive_json()

        # Resume `main` -> should emit resumed + live cockpit frame with session_id == sid
        ws.send_json({"type": "resume", "id": sid})
        f1 = ws.receive_json()
        assert f1["type"] == "resumed" and f1["id"] == sid
        f2 = ws.receive_json()
        assert f2["type"] == "cockpit"
        assert f2["session_id"] == sid
        assert f2["viewing_history"] is False

        # Querying /api/cockpit?session_id=<sid> while active MUST return live (not viewing_history)
        r = client.get(f"/api/cockpit?session_id={sid}")
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == sid
        assert body["viewing_history"] is False


def test_settings_general_and_plugin_config_endpoints(tmp_path, monkeypatch):
    import json

    fake_settings = tmp_path / "settings.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codelet.web.server._user_settings_file", lambda: fake_settings)
    monkeypatch.setattr("codelet.web.server._workspace_states_file", lambda: tmp_path / "workspace_states.json")

    app = create_app(auto_refresh=False)
    client = TestClient(app)

    # 1. GET /api/settings returns general defaults + plugins with declarative settings_schema
    init = client.get("/api/settings").json()
    assert "general" in init
    assert init["general"]["max_turns"] >= 1
    assert "plugins" in init
    plugins_by_name = {p["name"]: p for p in init["plugins"]}
    assert "rag" in plugins_by_name
    assert "sandbox" in plugins_by_name
    rag_keys = [f["key"] for f in plugins_by_name["rag"]["settings_schema"]]
    assert "inject" in rag_keys and "top_k" in rag_keys

    # 2. POST /api/settings/general persists and hot-applies to active connections
    with client.websocket_connect("/ws") as ws:
        for _ in range(8):
            ws.receive_json()

        r_gen = client.post(
            "/api/settings/general",
            json={
                "language": "en",
                "close_behavior": "quit",
                "max_turns": 45,
                "max_tokens": 16384,
                "context_window": 256000,
                "compact_threshold_ratio": 0.8,
                "compact_keep_recent": 8,
                "models_auto_refresh": False,
                "models_cache_ttl": 3600,
            },
        )
        assert r_gen.status_code == 200
        gen_body = r_gen.json()
        assert gen_body["ok"] is True
        assert gen_body["version"]
        assert gen_body["general"]["language"] == "en"
        assert gen_body["general"]["close_behavior"] == "quit"
        assert gen_body["general"]["max_turns"] == 45
        assert gen_body["general"]["max_tokens"] == 16384
        assert gen_body["general"]["context_window"] == 256000
        assert gen_body["general"]["compact_threshold_ratio"] == 0.8
        assert gen_body["general"]["compact_keep_recent"] == 8
        assert gen_body["general"]["models_auto_refresh"] is False
        assert gen_body["general"]["models_cache_ttl"] == 3600

        saved = json.loads(fake_settings.read_text(encoding="utf-8"))
        assert saved["language"] == "en"
        assert saved["close_behavior"] == "quit"
        assert saved["max_turns"] == 45
        assert saved["models_cache_ttl_seconds"] == 3600

        # 3. Enable `rag`, then POST /api/settings/plugin -> persists & hot-reloads active plugin
        ws.send_json({"type": "set_plugin_enabled", "name": "rag", "enabled": True})
        # Drain plugin enable response frames (plugins/tools/skills/commands/cockpit)
        seen_plugins = False
        for _ in range(6):
            frame = ws.receive_json()
            if frame.get("type") == "plugins":
                seen_plugins = True
                break
        assert seen_plugins

        r_pl = client.post(
            "/api/settings/plugin",
            json={"name": "rag", "config": {"inject": True, "top_k": 5}},
        )
        assert r_pl.status_code == 200
        pl_body = r_pl.json()
        assert pl_body["ok"] is True
        assert pl_body["reloaded"] is True
        rag_entry = next(p for p in pl_body["plugins"] if p["name"] == "rag")
        assert rag_entry["config"]["inject"] is True
        assert rag_entry["config"]["top_k"] == 5

        saved2 = json.loads(fake_settings.read_text(encoding="utf-8"))
        assert saved2["plugins"]["config"]["rag"] == {"inject": True, "top_k": 5}

        # Reset plugin config back to defaults ({})
        r_reset = client.post("/api/settings/plugin", json={"name": "rag", "config": {}})
        assert r_reset.status_code == 200
        saved3 = json.loads(fake_settings.read_text(encoding="utf-8"))
        assert "rag" not in saved3.get("plugins", {}).get("config", {})


def test_custom_plugin_settings_schema_discovered_in_settings_view(tmp_path, monkeypatch):
    from codelet.plugins.loader import list_available_plugins

    plugin_dir = tmp_path / ".codelet" / "plugins"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "my_custom.py").write_text(
        'class _MyPlugin:\n'
        '    name = "my_custom"\n'
        '    description = "Custom plugin with settings"\n'
        '    settings_schema = [\n'
        '        {"key": "endpoint", "label": "Endpoint", "type": "string", "default": "https://example.com"},\n'
        '        {"key": "retries", "label": "Retries", "type": "number", "default": 3, "min": 0, "max": 10},\n'
        '    ]\n'
        '    def setup(self, ctx):\n'
        '        pass\n'
        'PLUGIN = _MyPlugin()\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    items = list_available_plugins({"config": {"my_custom": {"retries": 7}}})
    entry = next(p for p in items if p["name"] == "my_custom")
    assert entry["builtin"] is False
    assert [f["key"] for f in entry["settings_schema"]] == ["endpoint", "retries"]
    assert entry["config"] == {"retries": 7}


def test_gen_release_notes_categorizes_english_changes():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "desktop" / "scripts" / "gen-release-notes.py"
    spec = importlib.util.spec_from_file_location("gen_release_notes", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    commits = [
        ("e65c091", "feat(settings): add About tab and language switcher"),
        ("e4f5a2b", "fix(llm): parse nested prompt_tokens_details cache write fields"),
        ("1234abc", "refactor(ui): simplify provider card layout"),
        ("8d1e6f9", "test: isolate global SESSION_DIR via conftest"),
    ]
    md = mod.build_release_notes("v0.1.4", "v0.1.3", commits)
    assert "## What's Changed in v0.1.4" in md
    assert "### ✨ New Features" in md
    assert "**settings**: Add About tab and language switcher (`e65c091`)" in md
    assert "### 🐛 Bug Fixes" in md
    assert "**llm**: Parse nested prompt_tokens_details cache write fields (`e4f5a2b`)" in md
    assert "### ⚡ Improvements" in md
    assert "**ui**: Simplify provider card layout (`1234abc`)" in md
    assert "### 📝 Docs & Tests" in md
    assert "### 📦 Downloads" in md
    assert "**Full Changelog**: https://github.com/muamuauh/codelet/compare/v0.1.3...v0.1.4" in md




