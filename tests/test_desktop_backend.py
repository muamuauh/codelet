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
