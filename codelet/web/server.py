"""FastAPI backend for the codelet web GUI.

One `AgentLoop` per WebSocket connection, wired with a `WebSocketSink` (every UI
event the loop emits becomes a JSON frame) and an async diff-approval callback
(the loop `await`s the browser's Approve/Reject click). REST endpoints expose the
static, read-mostly data the sidebar/panels need. Everything heavy is reused from
the core: config composition, session persistence, telemetry, slash commands.

Frames the server sends: text_delta, stream_end, tool_start, tool_result, notice,
thinking, permission_request, telemetry, turn_done, resumed, error.
Frames it receives: prompt, approve, reject, set_mode, set_profile, resume, slash.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from rich.console import Console

from ..agent_loop import AgentLoop
from ..cli import _build_config
from ..config import LLMProvider, PermissionMode
from ..llm.factory import build_client
from ..persistence.session import (
    SessionStore,
    list_project_sessions,
    list_sessions,
    load_session,
    restore_into,
)
from ..settings import load_env_files, load_settings, resolve_profile
from ..skills.loader import load_skills
from ..slash.loader import expand_command, load_commands

STATIC_DIR = Path(__file__).parent / "static"


class _Status:
    """No-op live status; the browser shows its own 'thinking' indicator."""

    def update(self, text: str) -> None:
        pass


class WebSocketSink:
    """AgentSink that turns every loop emission into a queued JSON frame.

    Sink methods run on the event-loop thread but WebSocket sends are async, so we
    only enqueue here; a single sender task drains the queue in order.
    """

    wants_stream = True

    def __init__(self, outbound: "asyncio.Queue[dict[str, Any]]") -> None:
        self._out = outbound

    def _emit(self, frame: dict[str, Any]) -> None:
        self._out.put_nowait(frame)

    def text_delta(self, text: str) -> None:
        self._emit({"type": "text_delta", "text": text})

    def text_block(self, text: str) -> None:
        # Non-streamed path (rare here): render identically to a delta.
        self._emit({"type": "text_delta", "text": text})

    def stream_end(self) -> None:
        self._emit({"type": "stream_end"})

    def tool_start(self, name: str, tool_input: dict[str, Any]) -> None:
        self._emit({"type": "tool_start", "name": name, "input": tool_input})

    def tool_result(self, name: str, output: str, is_error: bool) -> None:
        self._emit({"type": "tool_result", "name": name, "output": output, "is_error": is_error})

    def notice(self, message: str, *, level: str = "info") -> None:
        self._emit({"type": "notice", "message": message, "level": level})

    @contextlib.contextmanager
    def thinking(self):
        self._emit({"type": "thinking", "on": True})
        try:
            yield _Status()
        finally:
            self._emit({"type": "thinking", "on": False})


class Connection:
    """Per-WebSocket state: its agent, outbound queue, and pending approvals."""

    def __init__(self, ws: WebSocket, settings: dict[str, Any]) -> None:
        self.ws = ws
        self.settings = settings
        self.outbound: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
        self.sink = WebSocketSink(self.outbound)
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self.busy = False
        self.agent = self._build_agent(mode="ask")
        # Own session file per connection (auto-saved after each turn).
        self.store: SessionStore | None = None
        try:
            self.store = SessionStore(project_dir=Path.cwd() / ".codelet")
        except Exception:
            self.store = None

    # ---- agent wiring ----

    def _build_agent(self, *, profile: str | None = None, mode: str | None = None) -> AgentLoop:
        ns = SimpleNamespace(profile=profile, provider=None, model=None, base_url=None,
                             api_key=None, mode=mode, max_turns=None, no_stream=False)
        config = _build_config(ns, self.settings)
        quiet = Console(file=open(os.devnull, "w"))  # sink handles all output
        return AgentLoop(
            config=config,
            console=quiet,
            sink=self.sink,
            confirm_callback=self._confirm,
            skill_index=load_skills(),
        )

    async def _confirm(self, tool_name: str, diff: str) -> bool:
        """Async diff approval: ask the browser, await the click."""
        req_id = uuid.uuid4().hex[:8]
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        self.outbound.put_nowait({
            "type": "permission_request", "id": req_id, "tool": tool_name, "diff": diff,
        })
        try:
            return await fut
        finally:
            self.pending.pop(req_id, None)

    # ---- outbound pump ----

    async def sender(self) -> None:
        while True:
            frame = await self.outbound.get()
            await self.ws.send_json(frame)

    def send(self, frame: dict[str, Any]) -> None:
        self.outbound.put_nowait(frame)

    # ---- inbound handlers ----

    async def handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "prompt":
            await self._on_prompt(str(frame.get("text", "")))
        elif kind in ("approve", "reject"):
            fut = self.pending.get(str(frame.get("id")))
            if fut is not None and not fut.done():
                fut.set_result(kind == "approve")
        elif kind == "set_mode":
            self._set_mode(str(frame.get("mode", "")))
        elif kind == "set_profile":
            self._set_profile(str(frame.get("name", "")))
        elif kind == "resume":
            self._resume(str(frame.get("id", "")))
        elif kind == "slash":
            await self._on_slash(str(frame.get("line", "")))

    async def _on_prompt(self, text: str) -> None:
        if not text.strip():
            return
        if self.busy:
            self.send({"type": "notice", "message": "still working on the previous turn",
                       "level": "warn"})
            return
        self.busy = True
        try:
            await self.agent.run_async(text)
            self.send({"type": "telemetry", **self.agent.telemetry.snapshot()})
            if self.store is not None:
                with contextlib.suppress(Exception):
                    self.store.record(self.agent)
        except Exception as exc:  # never kill the socket on a turn error
            self.send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self.busy = False
            self.send({"type": "turn_done"})

    async def _on_slash(self, line: str) -> None:
        cmd = line.split(maxsplit=1)
        name = cmd[0].lstrip("/")
        rest = cmd[1] if len(cmd) > 1 else ""
        commands = load_commands()
        user_cmd = commands.get(name)
        if user_cmd is None:
            self.send({"type": "notice", "message": f"unknown command: /{name}", "level": "warn"})
            return
        await self._on_prompt(expand_command(user_cmd, rest))

    def _set_mode(self, mode: str) -> None:
        if mode in ("ask", "auto", "plan"):
            self.agent.config.permission_mode = PermissionMode(mode)
            self.send({"type": "profile", **self._profile_data()})

    def _set_profile(self, name: str) -> None:
        # Rebuild the client under the new profile, keep the conversation/context.
        prof = resolve_profile(self.settings, name or None)
        cfg = self.agent.config
        cfg.profile_name = prof.get("name")
        with contextlib.suppress(KeyError, ValueError):
            cfg.provider = LLMProvider(prof["provider"])
        if prof.get("model"):
            cfg.model = prof["model"]
        cfg.base_url = prof.get("base_url") or None
        cfg.api_key = prof.get("api_key") or cfg.api_key
        try:
            self.agent.client = build_client(cfg)
            self.send({"type": "profile", **self._profile_data()})
        except Exception as exc:
            self.send({"type": "error", "message": f"profile switch failed: {exc}"})

    def _resume(self, session_id: str) -> None:
        try:
            snapshot = load_session(session_id)
        except FileNotFoundError:
            self.send({"type": "error", "message": f"no session: {session_id}"})
            return
        except Exception as exc:
            self.send({"type": "error", "message": f"resume failed: {exc}"})
            return
        restore_into(self.agent, snapshot)
        self.send({"type": "resumed", "id": session_id,
                   "messages": _transcript(self.agent.context.messages)})

    def _profile_data(self) -> dict[str, Any]:
        c = self.agent.config
        return {
            "profile": c.profile_name, "provider": c.provider.value, "model": c.model,
            "base_url": c.base_url or "", "mode": c.permission_mode.value,
            "profiles": sorted((self.settings.get("profiles") or {}).keys()),
        }


def _transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten stored context messages into {role, text, tools} for re-rendering."""
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            out.append({"role": m["role"], "text": content, "tools": []})
            continue
        text_parts, tools = [], []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tools.append(block.get("name", "?"))
        if text_parts or tools:
            out.append({"role": m["role"], "text": "".join(text_parts), "tools": tools})
    return out


def create_app() -> FastAPI:
    load_env_files()
    settings = load_settings()

    app = FastAPI(title="codelet")
    app.state.settings = settings
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> Any:
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/api/profile")
    def api_profile() -> dict[str, Any]:
        ns = SimpleNamespace(profile=None, provider=None, model=None, base_url=None,
                             api_key=None, mode="ask", max_turns=None, no_stream=False)
        c = _build_config(ns, settings)
        return {
            "profile": c.profile_name, "provider": c.provider.value, "model": c.model,
            "base_url": c.base_url or "", "mode": c.permission_mode.value,
            "profiles": sorted((settings.get("profiles") or {}).keys()),
        }

    @app.get("/api/sessions")
    def api_sessions() -> dict[str, Any]:
        return {"project": list_project_sessions()[:20], "global": list_sessions()[:20]}

    @app.get("/api/tools")
    def api_tools() -> list[dict[str, str]]:
        from ..tools.base import ToolRegistry
        return [{"name": t.name, "description": t.description}
                for t in ToolRegistry.default().all_tools()]

    @app.get("/api/skills")
    def api_skills() -> list[dict[str, str]]:
        idx = load_skills()
        out = []
        for name in idx.names():
            s = idx.get(name)
            if s is not None:
                # source can be a Path -- stringify so response validation is happy.
                out.append({"name": name, "description": s.description, "source": str(s.source or "")})
        return out

    @app.get("/api/commands")
    def api_commands() -> list[dict[str, str]]:
        idx = load_commands()
        out = []
        for name in idx.names():
            c = idx.get(name)
            if c is not None:
                out.append({"name": name, "description": c.description or ""})
        return out

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        conn = Connection(websocket, settings)
        conn.send({"type": "profile", **conn._profile_data()})
        sender_task = asyncio.create_task(conn.sender())
        tasks: set[asyncio.Task] = set()
        try:
            while True:
                frame = await websocket.receive_json()
                # Prompts run as background tasks so approval frames can arrive
                # (and be handled) while a turn is in flight.
                if frame.get("type") in ("prompt", "slash"):
                    t = asyncio.create_task(conn.handle(frame))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                else:
                    await conn.handle(frame)
        except WebSocketDisconnect:
            pass
        finally:
            sender_task.cancel()
            for t in tasks:
                t.cancel()

    return app
