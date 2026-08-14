"""FastAPI backend for the codelet web GUI.

One `AgentLoop` per WebSocket connection, wired with a `WebSocketSink` (every UI
event the loop emits becomes a JSON frame) and an async diff-approval callback
(the loop `await`s the browser's Approve/Reject click). REST endpoints expose the
static, read-mostly data the sidebar/panels need. Everything heavy is reused from
the core: config composition, session persistence, telemetry, slash commands.

Frames the server sends: text_delta, stream_end, tool_start, tool_result, notice,
thinking, permission_request, telemetry, turn_done, resumed, profile, workspace,
tools, skills, cleared, sessions_changed, error. The `tools` frame is (re)sent on
connect, on a tool toggle, and after any turn that changed the tool set -- so a
self-authored tool (`create_tool`) appears in the sidebar immediately.
Frames it receives: prompt, approve, reject, set_mode, set_model, set_profile,
new_conversation, set_workspace, resume, slash.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import mimetypes
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from rich.console import Console

from ..agent_loop import AgentLoop
from ..cli import _build_config
from ..config import LLMProvider, PermissionMode
from ..llm.factory import build_client
from ..persistence.session import (
    SessionStore,
    delete_session,
    list_project_sessions,
    list_sessions,
    load_session,
    rename_session,
    restore_into,
)
from ..settings import load_env_files, load_settings, resolve_profile
from ..skills.loader import load_skills
from ..slash.loader import expand_command, load_commands
from ..system_prompt import build_system_prompt

STATIC_DIR = Path(__file__).parent / "static"


def _env_model_list() -> list[str]:
    """Models offered in the UI switcher, from `LLM_MODELS` in .env (comma-separated).

    With a proxy the base_url + api_key are fixed and only the model name changes,
    so this is a plain list -- e.g. LLM_MODELS=qwen/qwen3.7-max,moonshotai/kimi-k3.
    """
    raw = os.environ.get("LLM_MODELS", "")
    seen, out = set(), []
    for m in raw.split(","):
        m = m.strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


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
        # State that survives an agent rebuild (new chat / workspace switch).
        self.model: str | None = None      # None -> use the .env / profile default
        self.mode = "ask"
        self.workspace = os.getcwd()
        self.store: SessionStore | None = None
        self.agent: AgentLoop = None  # type: ignore[assignment]
        self._build()

    # ---- agent wiring ----

    def _build(self) -> None:
        """(Re)build the agent + session store in the current working directory,
        preserving the chosen model/mode. Picks up the cwd's .env/.codelet/skills."""
        ns = SimpleNamespace(profile=None, provider=None, model=self.model, base_url=None,
                             api_key=None, mode=self.mode, max_turns=None, no_stream=False)
        config = _build_config(ns, self.settings)
        # Remember the resolved default so the model dropdown can show it.
        self.model = config.model
        self.mode = config.permission_mode.value
        quiet = Console(file=open(os.devnull, "w"))  # sink handles all output
        self.agent = AgentLoop(
            config=config,
            console=quiet,
            sink=self.sink,
            confirm_callback=self._confirm,
            skill_index=load_skills(),
        )
        try:
            self.store = SessionStore(project_dir=Path.cwd() / ".codelet")
        except Exception:
            self.store = None

    def _env_models(self) -> list[str]:
        """Switchable models: the LLM_MODELS list from .env, plus the active one."""
        models = _env_model_list()
        cur = self.agent.config.model if self.agent else self.model
        if cur and cur not in models:
            models = [cur, *models]
        return models

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
            await self._on_prompt(str(frame.get("text", "")), frame.get("attachments") or [])
        elif kind in ("approve", "reject"):
            fut = self.pending.get(str(frame.get("id")))
            if fut is not None and not fut.done():
                fut.set_result(kind == "approve")
        elif kind == "set_mode":
            self._set_mode(str(frame.get("mode", "")))
        elif kind == "set_model":
            self._set_model(str(frame.get("model", "")))
        elif kind == "set_profile":
            self._set_profile(str(frame.get("name", "")))
        elif kind == "new_conversation":
            self._new_conversation()
        elif kind == "set_workspace":
            self._set_workspace(str(frame.get("path", "")))
        elif kind == "resume":
            self._resume(str(frame.get("id", "")))
        elif kind == "set_tool_enabled":
            self.agent.set_tool_enabled(str(frame.get("name", "")), bool(frame.get("enabled")))
            self.send({"type": "tools", "tools": self.agent.tools_state()})
        elif kind == "set_skill_enabled":
            self.agent.set_skill_enabled(str(frame.get("name", "")), bool(frame.get("enabled")))
            self.send({"type": "skills", "skills": self.agent.skills_state()})
        elif kind == "rename_session":
            self._rename_session(str(frame.get("id", "")), str(frame.get("title", "")))
        elif kind == "delete_session":
            self._delete_session(str(frame.get("id", "")))
        elif kind == "slash":
            await self._on_slash(str(frame.get("line", "")))

    def _rename_session(self, sid: str, title: str) -> None:
        if sid and title.strip():
            with contextlib.suppress(Exception):
                rename_session(sid, title.strip(), project_dir=Path.cwd() / ".codelet")
            self.send({"type": "sessions_changed"})

    def _delete_session(self, sid: str) -> None:
        if sid:
            with contextlib.suppress(Exception):
                delete_session(sid, project_dir=Path.cwd() / ".codelet")
            self.send({"type": "sessions_changed"})

    async def _on_prompt(self, text: str, attachments: list[dict[str, Any]] | None = None) -> None:
        attachments = attachments or []
        # Image attachments -> vision content blocks; other files -> a path note the
        # agent can read with its tools.
        images: list[dict[str, Any]] = []
        file_notes: list[str] = []
        for a in attachments:
            path = a.get("path")
            if not path:
                continue
            block = _image_block(path) if a.get("is_image") else None
            if block is not None:
                images.append(block)
            else:
                file_notes.append(path)
        prompt = text
        if file_notes:
            prompt = (text + "\n\n" if text else "") + \
                "[Attached files — read them if relevant:\n" + \
                "\n".join("- " + p for p in file_notes) + "\n]"

        if not prompt.strip() and not images:
            return
        if self.busy:
            self.send({"type": "notice", "message": "still working on the previous turn",
                       "level": "warn"})
            return
        self.busy = True
        # Snapshot the tool set so we can refresh the sidebar if the turn changed
        # it -- self-evolution (`create_tool`) registers a new tool mid-turn.
        tools_before = set(self.agent.registry.names())
        try:
            await self.agent.run_async(prompt, images=images or None)
            self.send({"type": "telemetry", **self.agent.telemetry.snapshot()})
            if self.store is not None:
                with contextlib.suppress(Exception):
                    self.store.record(self.agent)
        except Exception as exc:  # never kill the socket on a turn error
            self.send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self.busy = False
            # Push an updated tools frame only when the registry actually changed.
            if set(self.agent.registry.names()) != tools_before:
                self.send({"type": "tools", "tools": self.agent.tools_state()})
            self.send({"type": "turn_done"})

    async def _on_slash(self, line: str) -> None:
        cmd = line.split(maxsplit=1)
        name = cmd[0].lstrip("/")
        rest = cmd[1] if len(cmd) > 1 else ""
        # Plugin-registered commands run in-process and return a status string.
        plugin_result = self.agent.run_command(name, rest)
        if plugin_result is not None:
            self.send({"type": "notice", "message": plugin_result, "level": "info"})
            return
        user_cmd = load_commands().get(name)
        if user_cmd is None:
            self.send({"type": "notice", "message": f"unknown command: /{name}", "level": "warn"})
            return
        await self._on_prompt(expand_command(user_cmd, rest))

    def _refresh_system_prompt(self) -> None:
        """Rebuild the system prompt so its mode + model lines (and plugin
        sections) reflect the current config after a runtime mode/profile switch."""
        self.agent.rebuild_system_prompt()

    def _set_mode(self, mode: str) -> None:
        if mode in ("ask", "auto", "plan"):
            self.mode = mode
            self.agent.config.permission_mode = PermissionMode(mode)
            self._refresh_system_prompt()
            self.send({"type": "profile", **self._profile_data()})

    def _set_model(self, model: str) -> None:
        """Switch just the model. With a proxy the base_url/api_key are unchanged and
        the model name is passed per request, so no client rebuild is needed."""
        if not model:
            return
        self.model = model
        self.agent.config.model = model
        self._refresh_system_prompt()
        self.send({"type": "profile", **self._profile_data()})

    def _new_conversation(self) -> None:
        """Fresh agent + new session id in the same workspace, keeping model/mode."""
        self._build()
        self.send({"type": "cleared"})
        self.send({"type": "profile", **self._profile_data()})
        self.send({"type": "workspace", **self._workspace_data()})

    def _set_workspace(self, path: str) -> None:
        """Point the agent at another project directory: chdir, then rebuild so its
        file tools, .codelet skills/CLAUDE.md, and sessions all come from there."""
        p = Path(path).expanduser()
        if not p.is_dir():
            self.send({"type": "error", "message": f"not a directory: {path}"})
            return
        try:
            os.chdir(str(p))
        except OSError as exc:
            self.send({"type": "error", "message": f"cannot enter {path}: {exc}"})
            return
        self.workspace = os.getcwd()
        self._build()
        self.send({"type": "cleared"})
        self.send({"type": "workspace", **self._workspace_data()})
        self.send({"type": "profile", **self._profile_data()})
        self.send({"type": "sessions_changed"})

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
            self._refresh_system_prompt()
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
            "models": self._env_models(), "user": _current_user(),
            "profiles": sorted((self.settings.get("profiles") or {}).keys()),
        }

    def _workspace_data(self) -> dict[str, Any]:
        return {"cwd": self.workspace, "name": os.path.basename(self.workspace) or self.workspace}


def _current_user() -> str:
    """The signed-in user shown in the GUI. For now a default / env override;
    a real login flow can replace this (e.g. per-connection identity)."""
    return os.environ.get("CODELET_USER") or "muamuauh"


def _list_drives() -> list[str]:
    """Available drive roots (Windows: C:\\, D:\\, ...; POSIX: /)."""
    if os.name != "nt":
        return ["/"]
    import string
    return [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]


def _image_block(path: str) -> dict[str, Any] | None:
    """Read an image file into an Anthropic-shaped base64 image content block.
    Returns None if it can't be read (falls back to a path note)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    media = mimetypes.guess_type(path)[0] or "image/png"
    if not media.startswith("image/"):
        return None
    return {"type": "image", "source": {
        "type": "base64", "media_type": media,
        "data": base64.b64encode(data).decode("ascii"),
    }}


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
        models = _env_model_list()
        if c.model and c.model not in models:
            models = [c.model, *models]
        return {
            "profile": c.profile_name, "provider": c.provider.value, "model": c.model,
            "base_url": c.base_url or "", "mode": c.permission_mode.value,
            "models": models, "user": _current_user(),
            "profiles": sorted((settings.get("profiles") or {}).keys()),
        }

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

    @app.post("/api/upload")
    async def api_upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        """Save attachments into the current workspace's .codelet/uploads/<id>/ so the
        agent can read them by path. Returns absolute paths for prompt injection."""
        dest = Path.cwd() / ".codelet" / "uploads" / uuid.uuid4().hex[:8]
        dest.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in files:
            name = Path(f.filename or "file").name
            p = dest / name
            p.write_bytes(await f.read())
            saved.append({"name": name, "path": str(p.resolve()),
                          "is_image": p.suffix.lower() in _IMAGE_EXTS})
        return {"files": saved}

    DRIVES = "::drives::"

    @app.get("/api/browse")
    def api_browse(path: str = "") -> dict[str, Any]:
        """List subdirectories for the workspace picker. `entries` carry full
        paths so drive roots (E:\\, D:\\) are navigable, not just C:."""
        if path == DRIVES:
            entries = [{"name": d, "path": d} for d in _list_drives()]
            return {"path": "This PC", "parent": None, "entries": entries, "is_root": True}
        try:
            base = (Path(path).expanduser() if path else Path.home()).resolve()
            if not base.is_dir():
                base = Path.home().resolve()
            entries = sorted(
                ({"name": p.name, "path": str(p)}
                 for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")),
                key=lambda e: e["name"].lower(),
            )[:500]
        except Exception as exc:
            return {"path": str(Path.home()), "parent": DRIVES, "entries": [], "error": str(exc)}
        # ".." from a drive root (parent == self) goes to the drive list.
        parent = DRIVES if base.parent == base else str(base.parent)
        return {"path": str(base), "parent": parent, "entries": entries}

    @app.post("/api/shutdown")
    def api_shutdown() -> dict[str, str]:
        """Stop the server (the GUI's Quit button). Exits shortly after replying."""
        def _bye() -> None:
            import time
            time.sleep(0.3)
            os._exit(0)
        import threading
        threading.Thread(target=_bye, daemon=True).start()
        return {"status": "shutting down"}

    @app.get("/api/sessions")
    def api_sessions() -> dict[str, Any]:
        return {"project": list_project_sessions()[:20], "global": list_sessions()[:20]}

    @app.get("/api/tools")
    def api_tools() -> list[dict[str, str]]:
        from ..plugins.loader import apply_plugins
        from ..tools.base import ToolRegistry
        reg = ToolRegistry.default()
        with contextlib.suppress(Exception):
            apply_plugins(reg, settings.get("plugins") or {})  # reflect plugin tools
        return [{"name": t.name, "description": t.description} for t in reg.all_tools()]

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
        conn.send({"type": "workspace", **conn._workspace_data()})
        conn.send({"type": "tools", "tools": conn.agent.tools_state()})
        conn.send({"type": "skills", "skills": conn.agent.skills_state()})
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
