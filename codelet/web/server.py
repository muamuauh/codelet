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
import subprocess
import uuid
from datetime import datetime
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
from ..llm.models import (
    CatalogSnapshot,
    ModelCatalog,
    ModelInfo,
    normalize_base_url,
    resolve_ttl_seconds,
)
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
    """Optional pinned/fallback models from `LLM_MODELS` in .env (comma-separated).

    When the endpoint supports `GET /models` (e.g. ZenMux), live models are
    fetched and cached automatically; any ids still listed in `LLM_MODELS` are
    kept pinned at the top and serve as an offline fallback.
    """
    raw = os.environ.get("LLM_MODELS", "")
    seen, out = set(), []
    for m in raw.split(","):
        m = m.strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _compose_models(
    snap: CatalogSnapshot,
    current_model: str | None,
    *,
    ttl_seconds: int,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """Merge pinned env models + cached/remote catalog + active model into UI payloads."""
    by_id: dict[str, ModelInfo] = {m.id: m for m in snap.models}
    pinned = _env_model_list()
    pinned_set = set(pinned)

    ordered_ids: list[str] = []
    seen: set[str] = set()

    def _add(mid: str) -> None:
        if mid and mid not in seen:
            seen.add(mid)
            ordered_ids.append(mid)

    if current_model and current_model not in pinned_set and current_model not in by_id:
        _add(current_model)
    for mid in pinned:
        _add(mid)
    for m in snap.models:
        _add(m.id)
    if current_model:
        _add(current_model)

    entries: list[dict[str, Any]] = []
    for mid in ordered_ids:
        info = by_id.get(mid)
        if info is not None:
            row = info.to_dict()
        else:
            owned_by = mid.split("/", 1)[0] if "/" in mid else ""
            row = ModelInfo(id=mid, owned_by=owned_by).to_dict()
        row["pinned"] = mid in pinned_set
        entries.append(row)

    source = snap.source
    if source == "empty":
        source = "env" if pinned else "fallback"

    meta = {
        "source": source,
        "fetched_at": snap.fetched_at,
        "count": len(entries),
        "remote_count": len(snap.models),
        "ttl_seconds": ttl_seconds,
        "error": snap.error,
    }
    return ordered_ids, entries, meta


def _can_fetch_models(base_url: str | None, api_key: str | None, provider: str) -> bool:
    if normalize_base_url(base_url, provider=provider):
        if provider == "anthropic" and not base_url and not api_key:
            return False
        return True
    return False


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

    def reasoning_delta(self, text: str) -> None:
        self._emit({"type": "reasoning_delta", "text": text})

    def todos_updated(self, todos: list[dict[str, Any]]) -> None:
        self._emit({"type": "todos", "todos": todos})


_BUILTIN_SLASH_COMMANDS: list[dict[str, str]] = [
    {"name": "btw", "description": "Ask a quick side question in the Side panel (isolated context)", "source": "builtin"},
    {"name": "compact", "description": "Manually compact conversation context (optional focus note)", "source": "builtin"},
    {"name": "clear", "description": "Clear conversation and start a fresh chat", "source": "builtin"},
    {"name": "plan", "description": "Switch to read-only plan mode (optionally plan a task)", "source": "builtin"},
    {"name": "mode", "description": "Show or switch permission mode (normal|ask|plan|auto)", "source": "builtin"},
    {"name": "effort", "description": "Adjust thinking effort (auto|off|low|medium|high)", "source": "builtin"},
    {"name": "model", "description": "Open model picker, switch model id, or `/model refresh`", "source": "builtin"},
    {"name": "cockpit", "description": "Open the Cockpit monitoring drawer (`/cockpit refresh`)", "source": "builtin"},
    {"name": "diff", "description": "Open workspace Git Changes & Diff drawer", "source": "builtin"},
    {"name": "todos", "description": "Open the live Tasks checklist drawer", "source": "builtin"},
    {"name": "cost", "description": "Show session & 7-day spend + ZenMux balance", "source": "builtin"},
    {"name": "install", "description": "Install a Skill from a GitHub URL (`/install <url> [--global] [--plugin <pack>]`)", "source": "builtin"},
    {"name": "export", "description": "Download the current conversation as Markdown", "source": "builtin"},
    {"name": "help", "description": "List available slash commands", "source": "builtin"},
]


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:
        return None


def _git_changes(cwd: Path | None = None) -> dict[str, Any]:
    root_dir = cwd or Path.cwd()
    probe = _run_git(["rev-parse", "--is-inside-work-tree"], root_dir)
    if probe is None or probe.returncode != 0 or probe.stdout.strip().lower() != "true":
        return {"is_git": False, "branch": "", "files": []}

    branch_res = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root_dir)
    branch = branch_res.stdout.strip() if (branch_res and branch_res.returncode == 0) else ""

    status_res = _run_git(["status", "--porcelain", "-uall"], root_dir)
    if status_res is None or status_res.returncode != 0:
        return {"is_git": True, "branch": branch, "files": []}

    numstat: dict[str, tuple[int, int]] = {}
    for cmd in (["diff", "--numstat"], ["diff", "--cached", "--numstat"]):
        ns = _run_git(cmd, root_dir)
        if not ns or ns.returncode != 0:
            continue
        for line in ns.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            add_s, del_s, rel = parts[0], parts[1], parts[2].strip()
            a = int(add_s) if add_s.isdigit() else 0
            d = int(del_s) if del_s.isdigit() else 0
            prev_a, prev_d = numstat.get(rel, (0, 0))
            numstat[rel] = (prev_a + a, prev_d + d)

    files: list[dict[str, Any]] = []
    for raw in status_res.stdout.splitlines():
        if len(raw) < 4:
            continue
        code = raw[:2]
        rel = raw[3:].strip()
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip()
        if rel.startswith('"') and rel.endswith('"'):
            rel = rel[1:-1]
        kind = "untracked" if code == "??" else ("deleted" if "D" in code else ("added" if "A" in code else "modified"))
        added, deleted = numstat.get(rel, (0, 0))
        if kind == "untracked":
            try:
                p = (root_dir / rel).resolve()
                if p.is_file() and p.stat().st_size < 512_000:
                    added = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                pass
        files.append({"path": rel, "status": kind, "code": code.strip() or "M", "added": added, "deleted": deleted})

    return {"is_git": True, "branch": branch, "files": files[:200]}


def _git_file_diff(rel_path: str, cwd: Path | None = None) -> dict[str, Any]:
    root_dir = (cwd or Path.cwd()).resolve()
    rel = (rel_path or "").strip()
    if not rel:
        return {"path": "", "diff": "", "error": "empty path"}
    target = (root_dir / rel).resolve()
    try:
        target.relative_to(root_dir)
    except ValueError:
        return {"path": rel, "diff": "", "error": "path outside workspace"}

    # Try HEAD diff first (covers both staged + unstaged edits to tracked files).
    res = _run_git(["diff", "HEAD", "--", rel], root_dir)
    diff_text = res.stdout if (res and res.returncode == 0 and res.stdout.strip()) else ""
    if not diff_text:
        res2 = _run_git(["diff", "--", rel], root_dir)
        diff_text = res2.stdout if (res2 and res2.returncode == 0 and res2.stdout.strip()) else ""
    if not diff_text and target.is_file():
        # Untracked new file: synthesize a readable unified diff preview.
        try:
            raw_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            cap = raw_lines[:400]
            header = f"--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,{len(cap)} @@\n"
            body = "\n".join("+" + ln for ln in cap)
            if len(raw_lines) > 400:
                body += f"\n+... ({len(raw_lines) - 400} more lines truncated)"
            diff_text = header + body
        except OSError as exc:
            return {"path": rel, "diff": "", "error": str(exc)}
    return {"path": rel, "diff": diff_text or "(no diff)"}


class Connection:
    """Per-WebSocket state: its agent, outbound queue, and pending approvals."""

    def __init__(
        self,
        ws: WebSocket,
        settings: dict[str, Any],
        catalog: ModelCatalog | None = None,
    ) -> None:
        self.ws = ws
        self.settings = dict(settings)
        raw_plugins = settings.get("plugins") or {}
        self.settings["plugins"] = {
            **raw_plugins,
            "enabled": list(raw_plugins["enabled"]) if raw_plugins.get("enabled") is not None else None,
            "config": dict(raw_plugins.get("config") or {}),
        }
        self.catalog = catalog or ModelCatalog(ttl_seconds=resolve_ttl_seconds(settings))
        self.outbound: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
        self.sink = WebSocketSink(self.outbound)
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self.busy = False
        self._active_turn_task: asyncio.Task[Any] | None = None
        self._btw_history: list[dict[str, str]] = []
        # State that survives an agent rebuild (new chat / workspace switch).
        self.model: str | None = None      # None -> use the .env / profile default
        self.mode = "ask"
        self.effort = "auto"
        self.workspace = os.getcwd()
        self.store: SessionStore | None = None
        self.agent: AgentLoop = None  # type: ignore[assignment]
        self._last_catalog_error: str | None = None
        self._build()

    # ---- agent wiring ----

    def _build(self) -> None:
        """(Re)build the agent + session store in the current working directory,
        preserving the chosen model/mode. Picks up the cwd's .env/.codelet/skills."""
        ns = SimpleNamespace(profile=None, provider=None, model=self.model, base_url=None,
                             api_key=None, mode=self.mode, effort=self.effort, max_turns=None, no_stream=False)
        config = _build_config(ns, self.settings)
        # Remember the resolved default so the model dropdown can show it.
        self.model = config.model
        self.mode = config.permission_mode.value
        self.effort = config.reasoning_effort
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
        self._apply_catalog_metadata(self._catalog_snapshot())

    def _catalog_snapshot(self) -> CatalogSnapshot:
        c = self.agent.config
        snap = self.catalog.get_cached(c.base_url, provider=c.provider.value)
        if self._last_catalog_error and not snap.error:
            snap.error = self._last_catalog_error
        return snap

    def _apply_catalog_metadata(self, snap: CatalogSnapshot) -> None:
        """Apply discovered model pricing (without overriding explicit settings)
        and context_length for the active model."""
        if not snap.models or self.agent is None:
            return
        merged = snap.pricing_map()
        merged.update(self.settings.get("pricing") or {})
        self.agent.telemetry.update_pricing(merged)
        info = snap.get(self.agent.config.model)
        if info and info.context_length:
            self.agent.config.context_window = info.context_length

    async def refresh_models(self, *, force: bool = False, notify: bool = False) -> CatalogSnapshot:
        c = self.agent.config
        if not _can_fetch_models(c.base_url, c.api_key, c.provider.value):
            snap = CatalogSnapshot(error="No base_url configured for model refresh")
            if notify:
                self.send({"type": "notice", "message": snap.error, "level": "warn"})
            return snap
        snap = await self.catalog.arefresh(
            c.base_url, c.api_key, provider=c.provider.value, force=force
        )
        self._last_catalog_error = snap.error
        self._apply_catalog_metadata(snap)
        if notify or (snap.source == "remote" and snap.models):
            self.send({"type": "profile", **self._profile_data(snap)})
        if notify:
            if snap.error:
                self.send({"type": "notice", "message": f"Model refresh warning: {snap.error}",
                           "level": "warn"})
            else:
                self.send({"type": "notice",
                           "message": f"Updated {len(snap.models)} models from endpoint.",
                           "level": "info"})
        return snap

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
        elif kind == "set_effort":
            self._set_effort(str(frame.get("effort", "")))
        elif kind == "set_model":
            self._set_model(str(frame.get("model", "")))
        elif kind == "refresh_models":
            await self.refresh_models(force=True, notify=True)
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
        elif kind == "set_plugin_enabled":
            self._set_plugin_enabled(str(frame.get("name", "")), bool(frame.get("enabled")))
        elif kind == "rename_session":
            self._rename_session(str(frame.get("id", "")), str(frame.get("title", "")))
        elif kind == "delete_session":
            self._delete_session(str(frame.get("id", "")))
        elif kind == "slash":
            await self._on_slash(str(frame.get("line", "")))
        elif kind == "cancel":
            self._cancel_turn()
        elif kind == "btw_ask":
            await self._on_btw(
                str(frame.get("text", "")),
                include_context=bool(frame.get("include_context", True)),
            )
        elif kind == "btw_clear":
            self._btw_history.clear()
            self.send({"type": "btw_cleared"})
        elif kind == "get_cockpit":
            await self._send_cockpit(force_balance=False)
        elif kind == "render_panel":
            pid = str(frame.get("id", "")).strip()
            if pid:
                payload = self.agent.render_panel(pid)
                if payload is not None:
                    self.send({"type": "panel_data", **payload})
        elif kind == "refresh_balance":
            await self._send_cockpit(force_balance=True)
        elif kind == "set_context_limit":
            val = frame.get("limit")
            try:
                n = int(val)
                if n > 0:
                    self.agent.config.context_window = n
                else:
                    # 0 = Auto (restore from model catalog or settings default)
                    info = self._catalog_snapshot().get(self.agent.config.model)
                    self.agent.config.context_window = (
                        info.context_length if (info and info.context_length)
                        else int(self.settings.get("context_window") or 200_000)
                    )
            except (TypeError, ValueError):
                pass
            await self._send_cockpit(force_balance=False)
        elif kind == "compact":
            await self._on_compact(str(frame.get("focus", "")))

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
        self._active_turn_task = asyncio.current_task()
        # Snapshot tool, skill & panel sets so we can refresh the sidebar/inspector
        # if the turn changed them (`create_tool`, `create_plugin_pack`, `install_skill`, `skill-creator`).
        tools_before = set(self.agent.registry.names())
        skills_before = {(s["name"], s["source"], s.get("category", "")) for s in self.agent.skills_state()}
        panels_before = [p["id"] for p in self.agent.panels_state()]
        plugins_before = [p["name"] for p in self.agent.plugins_state() if p["enabled"]]
        try:
            await self.agent.run_async(prompt, images=images or None)
            self.send({"type": "telemetry", **self.agent.telemetry.snapshot()})
            if self.store is not None:
                with contextlib.suppress(Exception):
                    self.store.record(self.agent)
        except asyncio.CancelledError:
            self.send({"type": "turn_cancelled"})
            self.send({"type": "notice", "message": "Stopped by user.", "level": "warn"})
        except Exception as exc:  # never kill the socket on a turn error
            self.send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self.busy = False
            self._active_turn_task = None
            # Push updated capability frames only when the sets actually changed.
            if set(self.agent.registry.names()) != tools_before:
                self.send({"type": "tools", "tools": self.agent.tools_state()})
            skills_after = self.agent.skills_state()
            plugins_after = self.agent.plugins_state()
            if {(s["name"], s["source"], s.get("category", "")) for s in skills_after} != skills_before:
                self.send({"type": "skills", "skills": skills_after})
            if [p["name"] for p in plugins_after if p["enabled"]] != plugins_before:
                self.send({"type": "plugins", "plugins": plugins_after})
                self.send({"type": "commands", "commands": self._commands_data()})
            panels_after = self.agent.panels_state()
            if [p["id"] for p in panels_after] != panels_before:
                self.send({"type": "panels", "panels": panels_after})
            self.send({"type": "cockpit", **self._cockpit_snapshot()})
            self.send({"type": "turn_done"})

    def _cancel_turn(self) -> None:
        """Stop an in-flight main turn and reject any pending approval prompt."""
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_result(False)
        if self._active_turn_task is not None and not self._active_turn_task.done():
            self._active_turn_task.cancel()

    def _cockpit_snapshot(self, balance_view: dict[str, Any] | None = None) -> dict[str, Any]:
        rt = getattr(self.agent, "_cockpit", None)
        if rt is None:
            return {"enabled": False}
        return rt.snapshot(balance_view=balance_view)

    async def _send_cockpit(self, *, force_balance: bool = False) -> None:
        rt = getattr(self.agent, "_cockpit", None)
        if rt is None:
            self.send({"type": "cockpit", "enabled": False})
            return
        bal = None
        if force_balance:
            bal = await rt.balance.aget(self.agent.config.base_url, force=True)
        self.send({"type": "cockpit", **rt.snapshot(balance_view=bal)})

    async def _on_install_skill(self, args: str) -> None:
        from ..skills.installer import fetch_remote_skill, install_fetched_skill, parse_install_args

        parsed = parse_install_args(args)
        if not parsed["url"]:
            self.send({
                "type": "notice",
                "message": "Usage: /install <github-url> [--global] [--plugin <pack>] [--category <cat>] [--overwrite]",
                "level": "info",
            })
            self.send({"type": "turn_done"})
            return

        self.send({"type": "notice", "message": f"Installing skill from {parsed['url']}…", "level": "dim"})
        try:
            fetched = await asyncio.to_thread(fetch_remote_skill, parsed["url"])
            res = await asyncio.to_thread(
                install_fetched_skill,
                fetched,
                scope=parsed["scope"],
                plugin=parsed["plugin"],
                category=parsed["category"],
                name=parsed["name"],
                overwrite=parsed["overwrite"],
            )
            self.agent.reload_skills()
            self.send({"type": "skills", "skills": self.agent.skills_state()})
            self.send({"type": "plugins", "plugins": self.agent.plugins_state()})
            self.send({"type": "tools", "tools": self.agent.tools_state()})
            msg = f"Installed skill `{res.name}` → {res.target_path}"
            if res.warnings:
                msg += "\n" + "\n".join(f"⚠ {w}" for w in res.warnings)
            self.send({"type": "notice", "message": msg, "level": "warn" if res.warnings else "info"})
        except Exception as exc:
            self.send({"type": "error", "message": f"Install failed: {exc}"})
        finally:
            self.send({"type": "turn_done"})

    async def _on_compact(self, focus: str = "") -> None:
        if self.busy:
            self.send({"type": "notice", "message": "wait for the current turn before compacting",
                       "level": "warn"})
            return
        self.busy = True
        try:
            res = await self.agent.compact_now(focus=focus)
            if res["compacted"]:
                self.send({
                    "type": "notice",
                    "message": (
                        f"Compacted context: {res['before_tokens']:,} → {res['after_tokens']:,} tok "
                        f"({res['before_messages']} → {res['after_messages']} msgs)"
                    ),
                    "level": "info",
                })
            else:
                self.send({"type": "notice", "message": "Nothing to compact yet (conversation is short).",
                           "level": "dim"})
            self.send({"type": "cockpit", **self._cockpit_snapshot()})
        except Exception as exc:
            self.send({"type": "error", "message": f"Compact failed: {exc}"})
        finally:
            self.busy = False
            self.send({"type": "turn_done"})

    async def _on_btw(self, question: str, *, include_context: bool = True) -> None:
        """Isolated side-panel Q&A (`/btw`). Runs independently of `self.busy`."""
        q = question.strip()
        self.send({"type": "ui_action", "open_drawer": "btw"})
        if not q:
            return
        req_id = uuid.uuid4().hex[:8]
        self.send({"type": "btw_start", "id": req_id, "question": q})
        try:
            ans = await self.agent.ask_btw(
                q,
                history=self._btw_history,
                include_context=include_context,
                on_text=lambda d: self.send({"type": "btw_delta", "id": req_id, "text": d}),
            )
            self._btw_history.append({"role": "user", "text": q})
            self._btw_history.append({"role": "assistant", "text": ans})
            if len(self._btw_history) > 24:
                self._btw_history = self._btw_history[-24:]
            self.send({"type": "btw_done", "id": req_id, "text": ans})
            self.send({"type": "telemetry", **self.agent.telemetry.snapshot()})
            self.send({"type": "cockpit", **self._cockpit_snapshot()})
        except Exception as exc:
            self.send({"type": "btw_error", "id": req_id, "message": f"{type(exc).__name__}: {exc}"})

    async def _on_slash(self, line: str) -> None:
        cmd = line.split(maxsplit=1)
        name = cmd[0].lstrip("/").lower()
        rest = cmd[1].strip() if len(cmd) > 1 else ""

        if name == "btw":
            await self._on_btw(rest, include_context=True)
            self.send({"type": "turn_done"})
            return
        if name == "compact":
            await self._on_compact(rest)
            return
        if name in ("clear", "new"):
            self._new_conversation()
            self.send({"type": "turn_done"})
            return
        if name == "plan":
            self._set_mode("plan")
            if rest:
                await self._on_prompt(rest)
                return
            self.send({"type": "notice", "message": "Switched to plan (read-only) mode.", "level": "info"})
            self.send({"type": "turn_done"})
            return
        if name == "mode":
            if rest in ("normal", "ask", "auto", "plan"):
                self._set_mode(rest)
                self.send({"type": "notice", "message": f"Mode → {rest}", "level": "info"})
            else:
                self.send({"type": "notice",
                           "message": f"Current mode: {self.agent.config.permission_mode.value} (options: normal | ask | plan | auto)",
                           "level": "info"})
            self.send({"type": "turn_done"})
            return
        if name == "effort":
            eff = rest.lower()
            if eff in ("auto", "off", "low", "medium", "high"):
                self._set_effort(eff)
                self.send({"type": "notice", "message": f"Thinking effort → {eff}", "level": "info"})
            else:
                self.send({"type": "notice",
                           "message": f"Current thinking effort: {self.agent.config.reasoning_effort} (options: auto | off | low | medium | high)",
                           "level": "info"})
            self.send({"type": "turn_done"})
            return
        if name == "model":
            if not rest:
                self.send({"type": "ui_action", "open_modal": "model"})
            elif rest.lower() == "refresh":
                await self.refresh_models(force=True, notify=True)
            else:
                self._set_model(rest)
                self.send({"type": "notice", "message": f"Model → {rest}", "level": "info"})
            self.send({"type": "turn_done"})
            return
        if name == "diff":
            self.send({"type": "ui_action", "open_drawer": "changes"})
            self.send({"type": "turn_done"})
            return
        if name == "todos":
            self.send({"type": "todos", "todos": self.agent.todos_state()})
            self.send({"type": "ui_action", "open_drawer": "tasks"})
            self.send({"type": "turn_done"})
            return
        if name == "cockpit":
            self.send({"type": "ui_action", "open_drawer": "cockpit"})
            await self._send_cockpit(force_balance=("refresh" in rest.lower()))
            plugin_result = self.agent.run_command(name, rest)
            if plugin_result:
                self.send({"type": "notice", "message": plugin_result, "level": "info"})
            self.send({"type": "turn_done"})
            return
        if name in ("cost", "usage"):
            snap = self._cockpit_snapshot()
            sess = self.agent.telemetry.snapshot()["session"]
            sc = "n/a" if sess["cost_usd"] is None else f"${sess['cost_usd']:.4f}"
            msg = f"Session: {sess['input_tokens']:,} in · {sess['output_tokens']:,} out · {sc}"
            if snap.get("enabled"):
                u = snap["usage"]
                msg += f" | Today: ${u['today']['cost_usd']:.4f} | 7d: ${u['last_7d']['cost_usd']:.4f}"
                if snap["balance"].get("available"):
                    msg += f" | Balance: ${snap['balance']['total_credits']:.2f}"
                self.send({"type": "ui_action", "open_drawer": "cockpit"})
            self.send({"type": "notice", "message": msg, "level": "info"})
            self.send({"type": "turn_done"})
            return
        if name == "install":
            await self._on_install_skill(rest)
            return
        if name == "export":
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.send({
                "type": "export_markdown",
                "filename": f"codelet-{stamp}.md",
                "content": self.agent.export_markdown(),
            })
            self.send({"type": "notice", "message": f"Exported codelet-{stamp}.md", "level": "info"})
            self.send({"type": "turn_done"})
            return
        if name == "help":
            lines = [f"/{c['name']} — {c['description']}" for c in self._commands_data()]
            self.send({"type": "notice", "message": "\n".join(lines), "level": "info"})
            self.send({"type": "turn_done"})
            return

        # Plugin-registered commands run in-process and return a status string.
        plugin_result = self.agent.run_command(name, rest)
        if plugin_result is not None:
            self.send({"type": "notice", "message": plugin_result, "level": "info"})
            self.send({"type": "turn_done"})
            return
        user_cmd = load_commands().get(name)
        if user_cmd is None:
            self.send({"type": "notice", "message": f"unknown command: /{name}", "level": "warn"})
            self.send({"type": "turn_done"})
            return
        await self._on_prompt(expand_command(user_cmd, rest))

    def _refresh_system_prompt(self) -> None:
        """Rebuild the system prompt so its mode + model lines (and plugin
        sections) reflect the current config after a runtime mode/profile switch."""
        self.agent.rebuild_system_prompt()

    def _set_mode(self, mode: str) -> None:
        if mode in ("normal", "ask", "auto", "plan"):
            self.mode = mode
            self.agent.config.permission_mode = PermissionMode(mode)
            self._refresh_system_prompt()
            self.send({"type": "profile", **self._profile_data()})
            self.send({"type": "cockpit", **self._cockpit_snapshot()})

    def _set_effort(self, effort: str) -> None:
        eff = (effort or "").strip().lower()
        if eff in ("auto", "off", "low", "medium", "high"):
            self.effort = eff
            self.agent.config.reasoning_effort = eff
            self.send({"type": "profile", **self._profile_data()})
            self.send({"type": "cockpit", **self._cockpit_snapshot()})

    def _set_model(self, model: str) -> None:
        """Switch just the model. With a proxy the base_url/api_key are unchanged and
        the model name is passed per request, so no client rebuild is needed."""
        if not model:
            return
        self.model = model
        self.agent.config.model = model
        snap = self._catalog_snapshot()
        self._apply_catalog_metadata(snap)
        self._refresh_system_prompt()
        self.send({"type": "profile", **self._profile_data(snap)})
        self.send({"type": "cockpit", **self._cockpit_snapshot()})

    def _commands_data(self) -> list[dict[str, str]]:
        seen: set[str] = set()
        out: list[dict[str, str]] = []
        for row in _BUILTIN_SLASH_COMMANDS:
            seen.add(row["name"])
            out.append(dict(row))
        for row in self.agent.plugin_commands_state():
            if row["name"] not in seen:
                seen.add(row["name"])
                out.append(row)
        idx = load_commands()
        for name in idx.names():
            if name in seen:
                continue
            c = idx.get(name)
            if c is not None:
                seen.add(name)
                out.append({"name": name, "description": c.description or "user command", "source": "custom"})
        return out

    def _set_plugin_enabled(self, name: str, enabled: bool) -> None:
        """Hot-mount or hot-unmount a plugin in place via its reversible handle
        without rebuilding `AgentLoop` or losing any live session state."""
        if not name or self.busy:
            if self.busy:
                self.send({"type": "notice", "message": "wait for the current turn before toggling plugins",
                           "level": "warn"})
            return
        plugins_cfg = self.settings.setdefault("plugins", {})
        cur = plugins_cfg.get("enabled")
        if cur is None:
            cur = [p["name"] for p in self.agent.plugins_state() if p["enabled"]]
        else:
            cur = list(cur)

        if enabled:
            ok, msg = self.agent.mount_plugin(name)
            if not ok:
                self.send({"type": "notice", "message": msg, "level": "warn"})
            elif name not in cur:
                cur.append(name)
        else:
            unmounted = self.agent.unmount_plugin(name)
            for u in unmounted:
                if u in cur:
                    cur.remove(u)
            if len(unmounted) > 1:
                self.send({
                    "type": "notice",
                    "message": f"Disabled {', '.join(unmounted)} (dependent plugins cascaded).",
                    "level": "info",
                })

        plugins_cfg["enabled"] = cur
        self.agent.config.plugins["enabled"] = list(cur)

        self.send({"type": "plugins", "plugins": self.agent.plugins_state()})
        self.send({"type": "tools", "tools": self.agent.tools_state()})
        self.send({"type": "skills", "skills": self.agent.skills_state()})
        self.send({"type": "commands", "commands": self._commands_data()})
        self.send({"type": "panels", "panels": self.agent.panels_state()})
        self.send({"type": "cockpit", **self._cockpit_snapshot()})

    def _new_conversation(self) -> None:
        """Fresh agent + new session id in the same workspace, keeping model/mode."""
        with contextlib.suppress(Exception):
            self.agent._applied_plugins.dispose_all(self.agent.registry)
        self._build()
        self.send({"type": "cleared"})
        self.send({"type": "profile", **self._profile_data()})
        self.send({"type": "workspace", **self._workspace_data()})
        self.send({"type": "tools", "tools": self.agent.tools_state()})
        self.send({"type": "skills", "skills": self.agent.skills_state()})
        self.send({"type": "plugins", "plugins": self.agent.plugins_state()})
        self.send({"type": "commands", "commands": self._commands_data()})
        self.send({"type": "todos", "todos": self.agent.todos_state()})
        self.send({"type": "cockpit", **self._cockpit_snapshot()})
        self.send({"type": "panels", "panels": self.agent.panels_state()})

    def _set_workspace(self, path: str) -> None:
        """Point the agent at another project directory: chdir, then rebuild so its
        file tools, .codelet skills/CLAUDE.md, and sessions all come from there."""
        if not path.strip():   # e.g. "Open" clicked while at the drive list
            self.send({"type": "error", "message": "no workspace selected"})
            return
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
        self.send({"type": "tools", "tools": self.agent.tools_state()})
        self.send({"type": "skills", "skills": self.agent.skills_state()})
        self.send({"type": "plugins", "plugins": self.agent.plugins_state()})
        self.send({"type": "commands", "commands": self._commands_data()})
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
            snap = self._catalog_snapshot()
            self._apply_catalog_metadata(snap)
            self._refresh_system_prompt()
            self.send({"type": "profile", **self._profile_data(snap)})
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

    def _profile_data(self, snap: CatalogSnapshot | None = None) -> dict[str, Any]:
        c = self.agent.config
        snap = snap if snap is not None else self._catalog_snapshot()
        model_ids, model_catalog, models_meta = _compose_models(
            snap, c.model, ttl_seconds=self.catalog.ttl_seconds
        )
        return {
            "profile": c.profile_name, "provider": c.provider.value, "model": c.model,
            "base_url": c.base_url or "", "mode": c.permission_mode.value,
            "reasoning_effort": c.reasoning_effort,
            "models": model_ids, "model_catalog": model_catalog,
            "models_meta": models_meta, "user": _current_user(),
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


def create_app(
    *,
    catalog: ModelCatalog | None = None,
    auto_refresh: bool | None = None,
) -> FastAPI:
    load_env_files()
    settings = load_settings()
    model_catalog = catalog or ModelCatalog(ttl_seconds=resolve_ttl_seconds(settings))

    if auto_refresh is None:
        env_flag = os.environ.get("LLM_MODELS_AUTO_REFRESH", "").strip().lower()
        if env_flag in ("0", "false", "no", "off"):
            auto_refresh_enabled = False
        else:
            auto_refresh_enabled = bool(settings.get("models_auto_refresh", True))
    else:
        auto_refresh_enabled = auto_refresh

    connections: set[Connection] = set()

    def _default_config():
        ns = SimpleNamespace(profile=None, provider=None, model=None, base_url=None,
                             api_key=None, mode="ask", max_turns=None, no_stream=False)
        return _build_config(ns, settings)

    async def _periodic_refresh_loop() -> None:
        """Periodically refresh expired model catalogs while the server is running."""
        interval = max(60, min(model_catalog.ttl_seconds or 3600, 3600))
        while True:
            try:
                targets: dict[str, tuple[str | None, str | None, str]] = {}
                c = _default_config()
                if _can_fetch_models(c.base_url, c.api_key, c.provider.value):
                    key = normalize_base_url(c.base_url, provider=c.provider.value)
                    targets[key] = (c.base_url, c.api_key, c.provider.value)
                for conn in list(connections):
                    cc = conn.agent.config
                    if _can_fetch_models(cc.base_url, cc.api_key, cc.provider.value):
                        key = normalize_base_url(cc.base_url, provider=cc.provider.value)
                        targets[key] = (cc.base_url, cc.api_key, cc.provider.value)

                for key, (base_url, api_key, provider) in targets.items():
                    if not model_catalog.needs_refresh(base_url, provider=provider):
                        continue
                    snap = await model_catalog.arefresh(
                        base_url, api_key, provider=provider, force=False
                    )
                    if snap.source == "remote" and snap.models:
                        for conn in list(connections):
                            cc = conn.agent.config
                            if normalize_base_url(cc.base_url, provider=cc.provider.value) == key:
                                conn._apply_catalog_metadata(snap)
                                conn.send({"type": "profile", **conn._profile_data(snap)})
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(interval)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI):
        task: asyncio.Task[None] | None = None
        if auto_refresh_enabled:
            task = asyncio.create_task(_periodic_refresh_loop())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="codelet", lifespan=_lifespan)
    app.state.settings = settings
    app.state.catalog = model_catalog
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> Any:
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/api/profile")
    def api_profile() -> dict[str, Any]:
        c = _default_config()
        snap = model_catalog.get_cached(c.base_url, provider=c.provider.value)
        model_ids, model_entries, models_meta = _compose_models(
            snap, c.model, ttl_seconds=model_catalog.ttl_seconds
        )
        return {
            "profile": c.profile_name, "provider": c.provider.value, "model": c.model,
            "base_url": c.base_url or "", "mode": c.permission_mode.value,
            "models": model_ids, "model_catalog": model_entries,
            "models_meta": models_meta, "user": _current_user(),
            "profiles": sorted((settings.get("profiles") or {}).keys()),
        }

    @app.get("/api/models")
    def api_models() -> dict[str, Any]:
        c = _default_config()
        snap = model_catalog.get_cached(c.base_url, provider=c.provider.value)
        model_ids, model_entries, models_meta = _compose_models(
            snap, c.model, ttl_seconds=model_catalog.ttl_seconds
        )
        return {"models": model_entries, "model_ids": model_ids, "meta": models_meta}

    @app.post("/api/models/refresh")
    async def api_models_refresh() -> dict[str, Any]:
        c = _default_config()
        if not _can_fetch_models(c.base_url, c.api_key, c.provider.value):
            snap = CatalogSnapshot(error="No base_url configured for model refresh")
        else:
            snap = await model_catalog.arefresh(
                c.base_url, c.api_key, provider=c.provider.value, force=True
            )
            key = normalize_base_url(c.base_url, provider=c.provider.value)
            for conn in list(connections):
                cc = conn.agent.config
                if normalize_base_url(cc.base_url, provider=cc.provider.value) == key:
                    conn._last_catalog_error = snap.error
                    conn._apply_catalog_metadata(snap)
                    conn.send({"type": "profile", **conn._profile_data(snap)})
        model_ids, model_entries, models_meta = _compose_models(
            snap, c.model, ttl_seconds=model_catalog.ttl_seconds
        )
        return {"models": model_entries, "model_ids": model_ids, "meta": models_meta}

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
        paths so drive roots (E:\\, D:\\) are navigable, not just C:.

        No path (the picker's initial open) lists the drive roots rather than
        the home directory -- projects live all over the disk, so starting at
        the very top is one click to any drive instead of climbing out of ~."""
        if not path or path == DRIVES:
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

    @app.get("/api/plugins")
    def api_plugins() -> list[dict[str, Any]]:
        from ..plugins.loader import apply_plugins, list_available_plugins
        from ..tools.base import ToolRegistry
        reg = ToolRegistry.default()
        applied = None
        with contextlib.suppress(Exception):
            applied = apply_plugins(reg, settings.get("plugins") or {})
        return list_available_plugins(settings.get("plugins") or {}, applied=applied)

    @app.get("/api/skills")
    def api_skills() -> list[dict[str, Any]]:
        from ..plugins.loader import apply_plugins
        from ..tools.base import ToolRegistry
        idx = load_skills()
        reg = ToolRegistry.default()
        applied = None
        with contextlib.suppress(Exception):
            applied = apply_plugins(reg, settings.get("plugins") or {})
        enabled_set = set(applied.names if applied else [])
        for name in list(idx.names()):
            sk = idx.get(name)
            if sk is not None and sk.plugin and sk.plugin not in enabled_set:
                idx.remove(name)
        if applied:
            for sk in applied.skills:
                if sk.name not in idx.skills:
                    idx.add(sk)
        out = []
        for name in idx.names():
            s = idx.get(name)
            if s is not None:
                # source can be a Path -- stringify so response validation is happy.
                out.append({
                    "name": name,
                    "description": s.description,
                    "source": str(s.source or (f"plugin:{s.plugin}" if s.plugin else s.scope)),
                    "plugin": s.plugin,
                    "scope": s.scope,
                    "category": s.category,
                })
        return out

    @app.get("/api/commands")
    def api_commands() -> list[dict[str, str]]:
        idx = load_commands()
        out = [dict(c) for c in _BUILTIN_SLASH_COMMANDS]
        seen = {c["name"] for c in out}
        for name in idx.names():
            if name in seen:
                continue
            c = idx.get(name)
            if c is not None:
                out.append({"name": name, "description": c.description or "", "source": "custom"})
        return out

    @app.get("/api/cockpit")
    def api_cockpit() -> dict[str, Any]:
        for conn in list(connections):
            return conn._cockpit_snapshot()
        from ..plugins.builtin.cockpit import CockpitStore, ZenMuxBalanceClient
        return {
            "enabled": True,
            "usage": CockpitStore().summary(),
            "balance": ZenMuxBalanceClient().cached_view(),
        }

    @app.post("/api/cockpit/balance/refresh")
    async def api_cockpit_balance_refresh() -> dict[str, Any]:
        c = _default_config()
        from ..plugins.builtin.cockpit import ZenMuxBalanceClient
        if connections:
            last: dict[str, Any] = {}
            for conn in list(connections):
                await conn._send_cockpit(force_balance=True)
                last = conn._cockpit_snapshot()
            return last
        bal = await ZenMuxBalanceClient().aget(c.base_url, force=True)
        return {"balance": bal}

    @app.get("/api/git/changes")
    def api_git_changes() -> dict[str, Any]:
        return _git_changes()

    @app.get("/api/git/diff")
    def api_git_diff(path: str = "") -> dict[str, Any]:
        return _git_file_diff(path)

    @app.get("/api/panels")
    def api_panels() -> list[dict[str, Any]]:
        for conn in list(connections):
            return conn.agent.panels_state()
        return []

    @app.get("/api/panels/{panel_id}")
    def api_panel_data(panel_id: str) -> dict[str, Any]:
        for conn in list(connections):
            res = conn.agent.render_panel(panel_id)
            if res is not None:
                return res
        return {"id": panel_id, "error": "panel not found"}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        conn = Connection(websocket, settings, catalog=model_catalog)
        connections.add(conn)
        conn.send({"type": "profile", **conn._profile_data()})
        conn.send({"type": "workspace", **conn._workspace_data()})
        conn.send({"type": "tools", "tools": conn.agent.tools_state()})
        conn.send({"type": "skills", "skills": conn.agent.skills_state()})
        conn.send({"type": "plugins", "plugins": conn.agent.plugins_state()})
        conn.send({"type": "commands", "commands": conn._commands_data()})
        conn.send({"type": "todos", "todos": conn.agent.todos_state()})
        conn.send({"type": "cockpit", **conn._cockpit_snapshot()})
        if conn.agent.panels_state():
            conn.send({"type": "panels", "panels": conn.agent.panels_state()})
        sender_task = asyncio.create_task(conn.sender())
        tasks: set[asyncio.Task] = set()

        # Non-blocking refresh on connect if cache is missing or past TTL.
        if auto_refresh_enabled:
            c = conn.agent.config
            if _can_fetch_models(c.base_url, c.api_key, c.provider.value) and model_catalog.needs_refresh(
                c.base_url, provider=c.provider.value
            ):
                t = asyncio.create_task(conn.refresh_models(force=False, notify=False))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            rt = getattr(conn.agent, "_cockpit", None)
            if rt is not None and rt.balance.cached_view().get("configured"):
                tb = asyncio.create_task(conn._send_cockpit(force_balance=True))
                tasks.add(tb)
                tb.add_done_callback(tasks.discard)

        try:
            while True:
                frame = await websocket.receive_json()
                # Long-running frames run as background tasks so approvals, cancel,
                # and concurrent `/btw` side questions can flow freely.
                if frame.get("type") in ("prompt", "slash", "refresh_models", "btw_ask", "compact", "refresh_balance"):
                    t = asyncio.create_task(conn.handle(frame))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                else:
                    await conn.handle(frame)
        except WebSocketDisconnect:
            pass
        finally:
            connections.discard(conn)
            sender_task.cancel()
            for t in tasks:
                t.cancel()
            with contextlib.suppress(Exception):
                conn.agent._applied_plugins.dispose_all(conn.agent.registry)

    return app
