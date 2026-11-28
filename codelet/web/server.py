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
import hmac
import json
import mimetypes
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from rich.console import Console

from ..agent_loop import AgentLoop
from ..cli import _build_config
from ..config import Config, LLMProvider, PermissionMode
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
    list_sessions_by_workspace,
    load_session,
    rename_session,
    restore_into,
)
from ..settings import load_env_files, load_settings, resolve_profile
from ..skills.loader import load_skills
from ..slash.loader import expand_command, load_commands
from ..system_prompt import build_system_prompt

def _resolve_static_dir() -> Path:
    here = Path(__file__).resolve().parent / "static"
    if here.exists():
        return here
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / "codelet" / "web" / "static"
        if cand.exists():
            return cand
    return here


STATIC_DIR = _resolve_static_dir()
DESKTOP_COOKIE = "codelet_desktop_token"


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


def _normalize_profile_models(raw: Any) -> list[dict[str, str]]:
    """Normalize per-profile custom model directory entries into [{id, display_name}]."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            mid = item.strip()
            dname = mid
        elif isinstance(item, dict):
            mid = str(item.get("id") or "").strip()
            dname = str(item.get("display_name") or item.get("name") or "").strip() or mid
        else:
            continue
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid, "display_name": dname})
    return out


def _active_profile_models(settings: dict[str, Any], profile_name: str | None) -> list[dict[str, str]]:
    if not profile_name:
        return []
    profiles = settings.get("profiles") or {}
    entry = profiles.get(profile_name)
    if not isinstance(entry, dict):
        return []
    return _normalize_profile_models(entry.get("models"))


def _strip_html_to_text(raw: str, limit: int = 16000) -> str:
    import html as _html
    import re as _re

    text = _re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw or "")
    text = _re.sub(r"(?is)<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = _re.sub(r"[ \t\r\f\v]+", " ", text)
    text = _re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def _parse_discovered_models(text: str) -> list[dict[str, str]]:
    """Extract [{id, display_name}] robustly from an LLM JSON response."""
    import re as _re

    raw = (text or "").strip()
    if not raw:
        return []
    candidates = [raw]
    for m in _re.finditer(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=_re.IGNORECASE):
        candidates.insert(0, m.group(1).strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = raw.find(opener), raw.rfind(closer)
        if i != -1 and j > i:
            candidates.append(raw[i : j + 1])

    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except Exception:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("models"), list):
            res = _normalize_profile_models(parsed["models"])
            if res:
                return res
        elif isinstance(parsed, list):
            res = _normalize_profile_models(parsed)
            if res:
                return res
    return []


def _compose_models(
    snap: CatalogSnapshot,
    current_model: str | None,
    *,
    ttl_seconds: int,
    profile_models: list[dict[str, str]] | None = None,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """Merge profile custom directory + pinned env models + cached/remote catalog + active model."""
    by_id: dict[str, ModelInfo] = {m.id: m for m in snap.models}
    custom_list = _normalize_profile_models(profile_models)
    custom_map: dict[str, str] = {m["id"]: m["display_name"] for m in custom_list}
    pinned = _env_model_list()
    pinned_set = set(pinned) | set(custom_map.keys())

    ordered_ids: list[str] = []
    seen: set[str] = set()

    def _add(mid: str) -> None:
        if mid and mid not in seen:
            seen.add(mid)
            ordered_ids.append(mid)

    for cm in custom_list:
        _add(cm["id"])
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
        if mid in custom_map:
            row["display_name"] = custom_map[mid]
            row["custom"] = True
        row["pinned"] = mid in pinned_set
        entries.append(row)

    source = snap.source
    if source == "empty":
        source = "profile" if custom_list else ("env" if pinned else "fallback")

    meta = {
        "source": source,
        "fetched_at": snap.fetched_at,
        "count": len(entries),
        "remote_count": len(snap.models),
        "custom_count": len(custom_list),
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
    {"name": "cost", "description": "Show session, today & 7-day token and cost usage", "source": "builtin"},
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
        self._base_plugins_enabled: list[str] | None = (
            list(raw_plugins["enabled"]) if raw_plugins.get("enabled") is not None else None
        )
        self.settings["plugins"] = {
            **raw_plugins,
            "enabled": list(self._base_plugins_enabled) if self._base_plugins_enabled is not None else None,
            "config": dict(raw_plugins.get("config") or {}),
        }
        self._ws_ui_state: dict[str, Any] = {}
        self.catalog = catalog or ModelCatalog(ttl_seconds=resolve_ttl_seconds(settings))
        self.outbound: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
        self.sink = WebSocketSink(self.outbound)
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self.busy = False
        self._active_turn_task: asyncio.Task[Any] | None = None
        self._btw_history: list[dict[str, str]] = []
        # State that survives an agent rebuild (new chat / workspace switch).
        self.model: str | None = None      # None -> restore workspace/default pref or .env/profile default
        self.mode: str | None = None       # None -> restore workspace/default pref or "ask"
        self.effort: str | None = None     # None -> restore workspace/default pref or "auto"
        self.workspace = os.getcwd()
        self.store: SessionStore | None = None
        self.agent: AgentLoop = None  # type: ignore[assignment]
        self._last_catalog_error: str | None = None
        self._build()

    # ---- agent wiring ----

    def _build(self) -> None:
        """(Re)build the agent + session store in the current working directory,
        preserving or restoring per-workspace model/mode/effort/plugin/tool/skill states."""
        ws_state = _load_workspace_state(self.workspace)
        global_defaults = _load_workspace_state("__defaults__")
        self._ws_ui_state = dict(ws_state.get("ui") or {})
        plugins_cfg = self.settings.setdefault("plugins", {})
        if isinstance(ws_state.get("plugins_enabled"), list):
            plugins_cfg["enabled"] = [str(x) for x in ws_state["plugins_enabled"] if x]
        else:
            plugins_cfg["enabled"] = (
                list(self._base_plugins_enabled) if self._base_plugins_enabled is not None else None
            )

        saved_model = ws_state.get("model")
        if isinstance(saved_model, str) and saved_model.strip():
            resolved_model: str | None = saved_model.strip()
        elif self.model:
            resolved_model = self.model
        elif isinstance(global_defaults.get("model"), str) and global_defaults["model"].strip():
            resolved_model = global_defaults["model"].strip()
        else:
            resolved_model = None

        valid_modes = ("normal", "ask", "plan", "auto")
        saved_mode = str(ws_state.get("mode") or "").strip().lower()
        default_mode = str(global_defaults.get("mode") or "").strip().lower()
        if saved_mode in valid_modes:
            resolved_mode = saved_mode
        elif self.mode in valid_modes:
            resolved_mode = self.mode
        elif default_mode in valid_modes:
            resolved_mode = default_mode
        else:
            resolved_mode = "ask"

        valid_efforts = ("auto", "off", "low", "medium", "high")
        saved_effort = str(ws_state.get("reasoning_effort") or "").strip().lower()
        default_effort = str(global_defaults.get("reasoning_effort") or "").strip().lower()
        if saved_effort in valid_efforts:
            resolved_effort = saved_effort
        elif self.effort in valid_efforts:
            resolved_effort = self.effort
        elif default_effort in valid_efforts:
            resolved_effort = default_effort
        else:
            resolved_effort = "auto"

        ns = SimpleNamespace(profile=None, provider=None, model=resolved_model, base_url=None,
                             api_key=None, mode=resolved_mode, effort=resolved_effort, max_turns=None, no_stream=False)
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
        for tname in ws_state.get("disabled_tools") or []:
            if isinstance(tname, str) and tname:
                self.agent.set_tool_enabled(tname, False)
        for sname in ws_state.get("disabled_skills") or []:
            if isinstance(sname, str) and sname:
                self.agent.set_skill_enabled(sname, False)
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
            tools_st = self.agent.tools_state()
            _save_workspace_state(
                self.workspace,
                {"disabled_tools": sorted(t["name"] for t in tools_st if not t.get("enabled", True))},
            )
            self.send({"type": "tools", "tools": tools_st})
        elif kind == "set_skill_enabled":
            self.agent.set_skill_enabled(str(frame.get("name", "")), bool(frame.get("enabled")))
            skills_st = self.agent.skills_state()
            _save_workspace_state(
                self.workspace,
                {"disabled_skills": sorted(s["name"] for s in skills_st if not s.get("enabled", True))},
            )
            self.send({"type": "skills", "skills": skills_st})
        elif kind == "set_plugin_enabled":
            self._set_plugin_enabled(str(frame.get("name", "")), bool(frame.get("enabled")))
        elif kind == "set_ui_state":
            ui_patch: dict[str, Any] = {}
            if isinstance(frame.get("nav_open"), dict):
                ui_patch["nav_open"] = {str(k): bool(v) for k, v in frame["nav_open"].items()}
            if isinstance(frame.get("ws_groups_open"), dict):
                ui_patch["ws_groups_open"] = {str(k): bool(v) for k, v in frame["ws_groups_open"].items()}
            if ui_patch:
                saved = _save_workspace_state(self.workspace, {"ui": ui_patch})
                self._ws_ui_state = dict(saved.get("ui") or {})
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
        sid = self.store.id if self.store is not None else ""
        if rt is None:
            return {"enabled": False, "session_id": sid, "viewing_history": False}
        snap = rt.snapshot(balance_view=balance_view)
        snap["session_id"] = sid
        snap["viewing_history"] = False
        return snap

    async def _send_cockpit(self, *, force_balance: bool = False) -> None:
        rt = getattr(self.agent, "_cockpit", None)
        if rt is None:
            self.send({"type": "cockpit", **self._cockpit_snapshot()})
            return
        bal = None
        if force_balance:
            bal = await rt.balance.aget(self.agent.config.base_url, force=True)
        self.send({"type": "cockpit", **self._cockpit_snapshot(balance_view=bal)})

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
        mode = (mode or "").strip().lower()
        if mode in ("normal", "ask", "auto", "plan"):
            self.mode = mode
            self.agent.config.permission_mode = PermissionMode(mode)
            _save_workspace_state(self.workspace, {"mode": mode})
            _save_workspace_state("__defaults__", {"mode": mode})
            self._refresh_system_prompt()
            self.send({"type": "profile", **self._profile_data()})
            self.send({"type": "cockpit", **self._cockpit_snapshot()})

    def _set_effort(self, effort: str) -> None:
        eff = (effort or "").strip().lower()
        if eff in ("auto", "off", "low", "medium", "high"):
            self.effort = eff
            self.agent.config.reasoning_effort = eff
            _save_workspace_state(self.workspace, {"reasoning_effort": eff})
            _save_workspace_state("__defaults__", {"reasoning_effort": eff})
            self.send({"type": "profile", **self._profile_data()})
            self.send({"type": "cockpit", **self._cockpit_snapshot()})

    def _set_model(self, model: str) -> None:
        """Switch just the model. With a proxy the base_url/api_key are unchanged and
        the model name is passed per request, so no client rebuild is needed."""
        model = (model or "").strip()
        if not model:
            return
        self.model = model
        self.agent.config.model = model
        _save_workspace_state(self.workspace, {"model": model})
        _save_workspace_state("__defaults__", {"model": model})
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
        _save_workspace_state(self.workspace, {"plugins_enabled": list(cur)})

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
        snap_ws = (snapshot.get("workspace") or "").strip()
        if snap_ws and os.path.normcase(snap_ws) != os.path.normcase(self.workspace):
            wp = Path(snap_ws)
            if wp.is_dir():
                with contextlib.suppress(OSError):
                    os.chdir(str(wp))
                    self.workspace = os.getcwd()
                    self._build()
                    self.send({"type": "workspace", **self._workspace_data()})
                    self.send({"type": "profile", **self._profile_data()})
                    self.send({"type": "tools", "tools": self.agent.tools_state()})
                    self.send({"type": "skills", "skills": self.agent.skills_state()})
                    self.send({"type": "plugins", "plugins": self.agent.plugins_state()})
                    self.send({"type": "commands", "commands": self._commands_data()})
                    self.send({"type": "sessions_changed"})
        restore_into(self.agent, snapshot)
        self.store = SessionStore(session_id=session_id, project_dir=Path.cwd() / ".codelet")
        self.send({"type": "resumed", "id": session_id,
                   "messages": _transcript(self.agent.context.messages)})
        self.send({"type": "cockpit", **self._cockpit_snapshot()})

    def reload_settings(self, new_settings: dict[str, Any], *, force_model: str | None = None) -> None:
        """Hot-apply updated profile/API settings without losing current conversation."""
        prev_plugins = self.settings.get("plugins")
        self.settings = dict(new_settings)
        if prev_plugins is not None:
            self.settings["plugins"] = prev_plugins
        prof = resolve_profile(self.settings, None)
        cfg = self.agent.config
        cfg.profile_name = prof.get("name")
        with contextlib.suppress(KeyError, ValueError):
            cfg.provider = LLMProvider(prof["provider"])
        target_model = force_model or prof.get("model")
        if target_model:
            cfg.model = target_model
            self.model = target_model
            _save_workspace_state(self.workspace, {"model": target_model})
            _save_workspace_state("__defaults__", {"model": target_model})
        cfg.base_url = prof.get("base_url") or None
        cfg.api_key = prof.get("api_key") or None
        if prof.get("context_window"):
            cfg.context_window = int(prof["context_window"])
        with contextlib.suppress(Exception):
            self.agent.client = build_client(cfg)
        snap = self._catalog_snapshot()
        self._apply_catalog_metadata(snap)
        with contextlib.suppress(Exception):
            self._refresh_system_prompt()
        self.send({"type": "profile", **self._profile_data(snap)})

    def _profile_data(self, snap: CatalogSnapshot | None = None) -> dict[str, Any]:
        c = self.agent.config
        snap = snap if snap is not None else self._catalog_snapshot()
        model_ids, model_catalog, models_meta = _compose_models(
            snap,
            c.model,
            ttl_seconds=self.catalog.ttl_seconds,
            profile_models=_active_profile_models(self.settings, c.profile_name),
        )
        return {
            "profile": c.profile_name, "provider": c.provider.value, "model": c.model,
            "base_url": c.base_url or "", "mode": c.permission_mode.value,
            "reasoning_effort": c.reasoning_effort,
            "api_key_configured": bool(c.api_key),
            "models": model_ids, "model_catalog": model_catalog,
            "models_meta": models_meta, "user": _current_user(),
            "profiles": sorted((self.settings.get("profiles") or {}).keys()),
        }

    def _workspace_data(self) -> dict[str, Any]:
        return {
            "cwd": self.workspace,
            "name": os.path.basename(self.workspace) or self.workspace,
            "ui_state": dict(self._ws_ui_state),
        }


def _workspace_states_file() -> Path:
    return Path.home() / ".codelet" / "workspace_states.json"


def _workspace_state_key(workspace: str) -> str:
    if workspace == "__defaults__":
        return "__defaults__"
    try:
        norm = str(Path(workspace).resolve())
    except OSError:
        norm = workspace or ""
    return os.path.normcase(os.path.normpath(norm))


def _load_workspace_state(workspace: str) -> dict[str, Any]:
    path = _workspace_states_file()
    if not path.is_file():
        return {}
    try:
        all_states = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(all_states, dict):
            return {}
        entry = all_states.get(_workspace_state_key(workspace))
        return dict(entry) if isinstance(entry, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_workspace_state(workspace: str, patch: dict[str, Any]) -> dict[str, Any]:
    path = _workspace_states_file()
    key = _workspace_state_key(workspace)
    all_states: dict[str, Any] = {}
    if path.is_file():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                all_states = parsed
    entry = dict(all_states.get(key) or {}) if isinstance(all_states.get(key), dict) else {}
    for k, v in patch.items():
        if k == "ui" and isinstance(v, dict):
            cur_ui = dict(entry.get("ui") or {}) if isinstance(entry.get("ui"), dict) else {}
            for uk, uv in v.items():
                if isinstance(uv, dict) and isinstance(cur_ui.get(uk), dict):
                    merged = dict(cur_ui[uk])
                    merged.update(uv)
                    cur_ui[uk] = merged
                else:
                    cur_ui[uk] = uv
            entry["ui"] = cur_ui
        else:
            entry[k] = v
    all_states[key] = entry
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(all_states, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return entry


def _mask_secret(secret: str | None) -> str:
    s = (secret or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "••••"
    return f"{s[:3]}••••{s[-4:]}"


def _user_settings_file() -> Path:
    return Path.home() / ".codelet" / "settings.json"


def _read_user_settings_raw() -> dict[str, Any]:
    path = _user_settings_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_user_settings_raw(data: dict[str, Any]) -> None:
    path = _user_settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def _stringify_tool_result_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(p for p in parts if p)
    return str(content)


def _transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group stored context messages into UI turns with chronological `steps`
    (thinking, intermediate notes, tool calls + outputs) and final `text`."""
    tool_results: dict[str, dict[str, Any]] = {}
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = str(block.get("tool_use_id") or "")
                if tid:
                    tool_results[tid] = {
                        "output": _stringify_tool_result_content(block.get("content")),
                        "is_error": bool(block.get("is_error")),
                    }

    out: list[dict[str, Any]] = []
    active_assistant: dict[str, Any] | None = None

    for m in messages:
        role = m.get("role")
        content = m.get("content")

        if role == "user":
            if isinstance(content, str):
                active_assistant = None
                out.append({"role": "user", "text": content, "tools": [], "steps": []})
                continue
            text_parts: list[str] = []
            has_tool_result = False
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_result":
                    has_tool_result = True
                elif btype == "text" and block.get("text"):
                    text_parts.append(str(block.get("text")))
            if text_parts:
                active_assistant = None
                out.append({"role": "user", "text": "".join(text_parts), "tools": [], "steps": []})
            elif not has_tool_result:
                active_assistant = None
            continue

        if role == "assistant":
            if isinstance(content, str):
                if active_assistant is None:
                    active_assistant = {"role": "assistant", "text": content, "tools": [], "steps": []}
                    out.append(active_assistant)
                else:
                    if active_assistant["text"].strip():
                        active_assistant["steps"].append({"type": "note", "text": active_assistant["text"]})
                    active_assistant["text"] = content
                continue

            think_parts: list[str] = []
            text_parts = []
            tool_calls: list[dict[str, Any]] = []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype in ("thinking", "reasoning"):
                    t = str(block.get("thinking") or block.get("reasoning") or block.get("text") or "")
                    if t.strip():
                        think_parts.append(t)
                elif btype == "text":
                    t = str(block.get("text") or "")
                    if t:
                        text_parts.append(t)
                elif btype == "tool_use":
                    tid = str(block.get("id") or "")
                    name = str(block.get("name") or "?")
                    res = tool_results.get(tid) or {}
                    tool_calls.append({
                        "type": "tool",
                        "name": name,
                        "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                        "output": res.get("output", ""),
                        "is_error": bool(res.get("is_error", False)),
                    })

            if not (think_parts or text_parts or tool_calls):
                continue

            if active_assistant is None:
                active_assistant = {"role": "assistant", "text": "", "tools": [], "steps": []}
                out.append(active_assistant)
            elif active_assistant["text"].strip():
                active_assistant["steps"].append({"type": "note", "text": active_assistant["text"]})
                active_assistant["text"] = ""

            for t in think_parts:
                active_assistant["steps"].append({"type": "thinking", "text": t})

            joined_text = "".join(text_parts)
            if tool_calls:
                if joined_text.strip():
                    active_assistant["steps"].append({"type": "note", "text": joined_text})
                for tc in tool_calls:
                    active_assistant["steps"].append(tc)
                    active_assistant["tools"].append(tc["name"])
            else:
                active_assistant["text"] = joined_text
            continue

        # Any other role: preserve text safely
        active_assistant = None
        if isinstance(content, str):
            out.append({"role": role or "assistant", "text": content, "tools": [], "steps": []})

    return out


def create_app(
    *,
    catalog: ModelCatalog | None = None,
    auto_refresh: bool | None = None,
    desktop_token: str | None = None,
) -> FastAPI:
    load_env_files()
    settings = load_settings(user_path=_user_settings_file())
    model_catalog = catalog or ModelCatalog(ttl_seconds=resolve_ttl_seconds(settings))
    token = (desktop_token or "").strip() or None

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

    def _token_ok(candidate: str | None) -> bool:
        if not token:
            return True
        if not candidate:
            return False
        return hmac.compare_digest(candidate, token)

    app = FastAPI(title="codelet", lifespan=_lifespan)
    app.state.settings = settings
    app.state.catalog = model_catalog
    app.state.desktop_token = token

    if token:
        @app.middleware("http")
        async def _desktop_auth_middleware(request: Request, call_next: Any) -> Any:
            path = request.url.path
            if path in ("/", "/api/health") or path.startswith("/static/"):
                return await call_next(request)
            candidate = (
                request.cookies.get(DESKTOP_COOKIE)
                or request.headers.get("x-codelet-token")
                or request.query_params.get("desktop_token")
            )
            if not _token_ok(candidate):
                return JSONResponse({"detail": "desktop token required"}, status_code=403)
            return await call_next(request)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/api/health")
    def api_health() -> dict[str, Any]:
        return {
            "ok": True,
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "desktop": bool(token),
        }

    @app.get("/")
    def index(request: Request) -> Any:
        if token:
            q_tok = request.query_params.get("desktop_token")
            if q_tok and _token_ok(q_tok):
                resp = RedirectResponse(url="/", status_code=302)
                resp.set_cookie(
                    key=DESKTOP_COOKIE,
                    value=token,
                    httponly=True,
                    samesite="strict",
                    path="/",
                )
                return resp
            if not _token_ok(request.cookies.get(DESKTOP_COOKIE)):
                return JSONResponse({"detail": "desktop token required"}, status_code=403)
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/api/profile")
    def api_profile() -> dict[str, Any]:
        c = _default_config()
        snap = model_catalog.get_cached(c.base_url, provider=c.provider.value)
        model_ids, model_entries, models_meta = _compose_models(
            snap,
            c.model,
            ttl_seconds=model_catalog.ttl_seconds,
            profile_models=_active_profile_models(settings, c.profile_name),
        )
        return {
            "profile": c.profile_name, "provider": c.provider.value, "model": c.model,
            "base_url": c.base_url or "", "mode": c.permission_mode.value,
            "api_key_configured": bool(c.api_key),
            "models": model_ids, "model_catalog": model_entries,
            "models_meta": models_meta, "user": _current_user(),
            "profiles": sorted((settings.get("profiles") or {}).keys()),
        }

    def _settings_view() -> dict[str, Any]:
        c = _default_config()
        prof_res = resolve_profile(settings, None)
        raw_profiles = settings.get("profiles") or {}
        profiles_out: dict[str, dict[str, Any]] = {}
        for pname, pentry in raw_profiles.items():
            if not isinstance(pentry, dict):
                continue
            r = resolve_profile(settings, pname)
            profiles_out[pname] = {
                "name": pname,
                "provider": r.get("provider") or "openai",
                "base_url": r.get("base_url") or "",
                "model": r.get("model") or "",
                "api_key_env": r.get("api_key_env") or "LLM_API_KEY",
                "api_key_configured": bool(r.get("api_key")),
                "api_key_preview": _mask_secret(r.get("api_key")),
                "models": _normalize_profile_models(pentry.get("models")),
            }
        active_name = prof_res.get("name") or "default"
        if active_name not in profiles_out:
            profiles_out[active_name] = {
                "name": active_name,
                "provider": prof_res.get("provider") or "openai",
                "base_url": prof_res.get("base_url") or "",
                "model": prof_res.get("model") or c.model or "",
                "api_key_env": prof_res.get("api_key_env") or "LLM_API_KEY",
                "api_key_configured": bool(prof_res.get("api_key")),
                "api_key_preview": _mask_secret(prof_res.get("api_key")),
                "models": [],
            }
        return {
            "active_profile": active_name,
            "provider": c.provider.value,
            "base_url": c.base_url or "",
            "model": c.model,
            "api_key_env": prof_res.get("api_key_env") or "LLM_API_KEY",
            "api_key_configured": bool(c.api_key),
            "api_key_preview": _mask_secret(c.api_key),
            "profiles": profiles_out,
            "desktop": bool(token),
        }

    @app.get("/api/settings")
    def api_get_settings() -> dict[str, Any]:
        return _settings_view()

    @app.post("/api/settings/apply")
    async def api_apply_settings(request: Request) -> Any:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "Body must be an object"}, status_code=400)

        user_raw = _read_user_settings_raw()
        user_profiles = dict(user_raw.get("profiles") or {})

        delete_name = str(body.get("delete_profile") or "").strip()
        if delete_name:
            user_profiles.pop(delete_name, None)
            user_raw["profiles"] = user_profiles
            if user_raw.get("profile") == delete_name:
                next_name = next(iter(user_profiles.keys()), None)
                if next_name:
                    user_raw["profile"] = next_name
                else:
                    user_raw.pop("profile", None)
            _write_user_settings_raw(user_raw)
            fresh = load_settings(user_path=_user_settings_file())
            settings.clear()
            settings.update(fresh)
            app.state.settings = settings
            for conn in list(connections):
                conn.reload_settings(settings)
            return {"ok": True, **_settings_view()}

        profile_name = str(body.get("profile") or "default").strip() or "default"
        if profile_name == "_env":
            profile_name = "default"
        provider = str(body.get("provider") or "openai").strip().lower()
        if provider not in ("openai", "anthropic"):
            return JSONResponse({"ok": False, "error": f"Unsupported provider: {provider}"}, status_code=400)

        base_url = str(body.get("base_url") or "").strip() or None
        model = str(body.get("model") or "").strip() or None
        default_env = "ANTHROPIC_API_KEY" if provider == "anthropic" else "LLM_API_KEY"
        api_key_env = str(body.get("api_key_env") or default_env).strip() or default_env
        if not api_key_env.isidentifier():
            return JSONResponse({"ok": False, "error": f"Invalid env var name: {api_key_env}"}, status_code=400)

        clear_key = bool(body.get("clear_api_key"))
        raw_api_key = body.get("api_key")
        if clear_key:
            os.environ.pop(api_key_env, None)
        elif isinstance(raw_api_key, str) and raw_api_key.strip():
            # Keep API key strictly in memory (Desktop persists encrypted via DPAPI).
            os.environ[api_key_env] = raw_api_key.strip()

        # Persist ONLY non-secret profile metadata to ~/.codelet/settings.json.
        existing = dict(user_profiles.get(profile_name) or {})
        existing.pop("api_key", None)
        existing["provider"] = provider
        if base_url:
            existing["base_url"] = base_url
        else:
            existing.pop("base_url", None)
        if model:
            existing["model"] = model
        else:
            existing.pop("model", None)
        existing["api_key_env"] = api_key_env
        if "models" in body:
            custom_models = _normalize_profile_models(body.get("models"))
            if custom_models:
                existing["models"] = custom_models
            else:
                existing.pop("models", None)

        user_profiles[profile_name] = existing
        user_raw["profiles"] = user_profiles
        user_raw["profile"] = profile_name
        _write_user_settings_raw(user_raw)

        fresh = load_settings(user_path=_user_settings_file())
        settings.clear()
        settings.update(fresh)
        app.state.settings = settings

        if model:
            _save_workspace_state(os.getcwd(), {"model": model})
            _save_workspace_state("__defaults__", {"model": model})

        for conn in list(connections):
            conn.reload_settings(settings, force_model=model)
            c = conn.agent.config
            if _can_fetch_models(c.base_url, c.api_key, c.provider.value):
                with contextlib.suppress(Exception):
                    await conn.refresh_models(force=True, notify=False)

        return {"ok": True, **_settings_view()}

    @app.post("/api/settings/test")
    async def api_test_settings(request: Request) -> Any:
        import time as _time
        import httpx

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "Body must be an object"}, status_code=400)

        provider = str(body.get("provider") or "openai").strip().lower()
        base_url = str(body.get("base_url") or "").strip()
        api_key_env = str(body.get("api_key_env") or "").strip()
        api_key = str(body.get("api_key") or "").strip()
        if not api_key and api_key_env:
            api_key = (os.environ.get(api_key_env) or "").strip()
        if not api_key:
            api_key = (_default_config().api_key or "").strip()
        if not api_key:
            return {"ok": False, "error": "尚未输入或保存 API Key"}

        if provider == "anthropic":
            root = (base_url or "https://api.anthropic.com").rstrip("/")
            url = f"{root}/models" if root.endswith("/v1") else f"{root}/v1/models"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "accept": "application/json",
            }
        else:
            root = (base_url or "https://api.openai.com/v1").rstrip("/")
            url = f"{root}/models"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "accept": "application/json",
            }

        t0 = _time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
            ms = int((_time.perf_counter() - t0) * 1000)
            if resp.status_code == 200:
                models_count: int | None = None
                with contextlib.suppress(Exception):
                    payload = resp.json()
                    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                        models_count = len(payload["data"])
                    elif isinstance(payload, list):
                        models_count = len(payload)
                return {
                    "ok": True,
                    "latency_ms": ms,
                    "models_count": models_count,
                    "message": (
                        f"连接成功 · 发现 {models_count} 个模型 · {ms} ms"
                        if models_count is not None
                        else f"连接成功 · {ms} ms"
                    ),
                }
            if resp.status_code in (401, 403):
                return {"ok": False, "latency_ms": ms, "error": f"鉴权失败 (HTTP {resp.status_code})，请检查 API Key"}
            return {"ok": False, "latency_ms": ms, "error": f"接口返回 HTTP {resp.status_code} ({url})"}
        except Exception as exc:
            ms = int((_time.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": ms, "error": f"连接失败: {type(exc).__name__}: {exc}"}

    @app.post("/api/settings/discover-models")
    async def api_discover_models(request: Request) -> Any:
        """Discover available models for a relay/provider via /models API and/or
        reading the relay's documentation using the currently configured model."""
        from urllib.parse import urlparse
        import httpx

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "Body must be an object"}, status_code=400)

        provider = str(body.get("provider") or "openai").strip().lower()
        base_url = str(body.get("base_url") or "").strip()
        model = str(body.get("model") or "").strip()
        api_key_env = str(body.get("api_key_env") or "").strip()
        docs_url = str(body.get("docs_url") or "").strip()
        use_llm = bool(body.get("use_llm", True))

        api_key = str(body.get("api_key") or "").strip()
        if not api_key and api_key_env:
            api_key = (os.environ.get(api_key_env) or "").strip()
        if not api_key:
            api_key = (_default_config().api_key or "").strip()
        if not api_key:
            return {"ok": False, "error": "请先填写并保存 API Key"}

        # Stage 1: direct /models API probe
        api_models: list[dict[str, str]] = []
        if provider == "anthropic":
            root = (base_url or "https://api.anthropic.com").rstrip("/")
            models_url = f"{root}/models" if root.endswith("/v1") else f"{root}/v1/models"
            hdrs = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "accept": "application/json"}
        else:
            root = (base_url or "https://api.openai.com/v1").rstrip("/")
            models_url = f"{root}/models"
            hdrs = {"Authorization": f"Bearer {api_key}", "accept": "application/json"}

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            with contextlib.suppress(Exception):
                resp = await client.get(models_url, headers=hdrs)
                if resp.status_code == 200:
                    payload = resp.json()
                    rows = payload.get("data") if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
                    for row in rows or []:
                        if isinstance(row, dict) and row.get("id"):
                            mid = str(row["id"]).strip()
                            dname = str(row.get("display_name") or row.get("name") or "").strip() or mid
                            api_models.append({"id": mid, "display_name": dname})
                        elif isinstance(row, str) and row.strip():
                            api_models.append({"id": row.strip(), "display_name": row.strip()})
                    api_models = _normalize_profile_models(api_models)

            if not use_llm:
                if api_models:
                    return {
                        "ok": True,
                        "models": api_models,
                        "source": "api",
                        "message": f"已从接口拉取 {len(api_models)} 个模型",
                    }
                return {
                    "ok": False,
                    "error": f"接口 {models_url} 未返回模型列表，可尝试点击「AI 读文档发现」",
                }

            # Stage 2: fetch relay documentation pages in parallel
            cand_urls: list[str] = []
            if docs_url:
                cand_urls.append(docs_url)
            parsed_base = urlparse(base_url) if base_url else None
            if parsed_base and parsed_base.scheme in ("http", "https") and parsed_base.netloc:
                origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
                for suffix in ("/llms.txt", "/docs", "/models", "/pricing", "/"):
                    u = origin + suffix
                    if u not in cand_urls:
                        cand_urls.append(u)

            async def _fetch_doc(u: str) -> tuple[str, str]:
                try:
                    r = await client.get(
                        u,
                        headers={"User-Agent": "codelet-desktop/0.1", "Accept": "text/html,text/plain,application/json"},
                        timeout=7.0,
                    )
                    if r.status_code == 200 and r.text:
                        txt = _strip_html_to_text(r.text, limit=7000)
                        if len(txt) >= 60:
                            return u, txt
                except Exception:
                    pass
                return u, ""

            doc_chunks: list[str] = []
            docs_fetched: list[str] = []
            if cand_urls:
                fetched = await asyncio.gather(*(_fetch_doc(u) for u in cand_urls[:5]))
                total_len = 0
                for u, txt in fetched:
                    if not txt:
                        continue
                    docs_fetched.append(u)
                    room = max(0, 15000 - total_len)
                    if room < 200:
                        break
                    snippet = txt[:room]
                    doc_chunks.append(f"=== Source: {u} ===\n{snippet}")
                    total_len += len(snippet)

        if not doc_chunks and not api_models:
            return {
                "ok": False,
                "error": "未能从接口或默认文档路径读取到模型信息，请填写该中转站的模型文档 URL 后重试。",
            }

        target_model = model or _default_config().model
        if not target_model:
            return {
                "ok": True if api_models else False,
                "models": api_models,
                "source": "api",
                "docs_fetched": docs_fetched,
                "message": f"未指定默认模型，已直接返回接口的 {len(api_models)} 个模型" if api_models else "请先填写默认模型",
            }

        # Stage 3: ask the configured model to read docs + /models list and output structured JSON
        context_parts: list[str] = []
        if api_models:
            sample_ids = "\n".join(f"- {m['id']}" for m in api_models[:160])
            context_parts.append(f"Models returned by GET /models ({len(api_models)} total):\n{sample_ids}")
        if doc_chunks:
            context_parts.append("Documentation excerpts fetched from the relay:\n" + "\n\n".join(doc_chunks))

        sys_prompt = (
            "You extract available LLM models from an API relay's documentation and/or /models endpoint. "
            "Respond ONLY with valid JSON of the shape:\n"
            '{"models": [{"id": "<exact_api_model_id>", "display_name": "<concise_readable_name>"}]}\n'
            "Rules:\n"
            "1. Keep exact API model IDs verbatim (never invent IDs).\n"
            "2. Provide clean, human-readable display_name values (e.g. 'DeepSeek-V3' for 'deepseek-chat').\n"
            "3. Deduplicate by id and order the most notable/flagship coding models first (up to 80 entries).\n"
            "4. Output ONLY JSON with no surrounding explanation."
        )
        user_msg = (
            f"Relay base_url: {base_url or '(default)'}\n\n"
            + "\n\n".join(context_parts)
        )

        llm_models: list[dict[str, str]] = []
        llm_err: str | None = None
        try:
            tmp_cfg = Config(
                provider=LLMProvider(provider) if provider in ("openai", "anthropic") else LLMProvider.OPENAI,
                model=target_model,
                base_url=base_url or None,
                api_key=api_key,
                stream=False,
                max_tokens=4096,
            )
            llm_client = build_client(tmp_cfg)
            resp_obj = await asyncio.to_thread(
                llm_client.chat,
                messages=[{"role": "user", "content": user_msg}],
                system=sys_prompt,
                tools=[],
                model=target_model,
                max_tokens=4096,
            )
            raw_out = "".join(resp_obj.text_blocks or [])
            llm_models = _parse_discovered_models(raw_out)
        except Exception as exc:
            llm_err = f"{type(exc).__name__}: {exc}"

        # Merge LLM curated list first, then any remaining /models entries not covered
        merged: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for m in llm_models + api_models:
            if m["id"] not in seen_ids:
                seen_ids.add(m["id"])
                merged.append(m)

        if not merged:
            return {
                "ok": False,
                "docs_fetched": docs_fetched,
                "error": f"未能提取到可用模型{(' (' + llm_err + ')') if llm_err else '，建议直接粘贴该中转站的模型列表文档网址重试'}",
            }

        if llm_models and api_models:
            src = "docs+api" if docs_fetched else "llm+api"
            msg = f"已通过 {target_model} 整理并拉取 {len(merged)} 个可用模型"
        elif llm_models:
            src = "docs"
            msg = f"已通过 {target_model} 阅读文档提取 {len(merged)} 个可用模型"
        else:
            src = "api"
            msg = f"已从接口直接拉取 {len(merged)} 个模型" + (f"（文档解析回退: {llm_err}）" if llm_err else "")

        return {
            "ok": True,
            "models": merged,
            "source": src,
            "docs_fetched": docs_fetched,
            "message": msg,
        }

    @app.get("/api/models")
    def api_models() -> dict[str, Any]:
        c = _default_config()
        snap = model_catalog.get_cached(c.base_url, provider=c.provider.value)
        model_ids, model_entries, models_meta = _compose_models(
            snap,
            c.model,
            ttl_seconds=model_catalog.ttl_seconds,
            profile_models=_active_profile_models(settings, c.profile_name),
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
            snap,
            c.model,
            ttl_seconds=model_catalog.ttl_seconds,
            profile_models=_active_profile_models(settings, c.profile_name),
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
        """Stop the server (the GUI's Quit button). Cleans up active plugins first."""
        for conn in list(connections):
            with contextlib.suppress(Exception):
                conn.agent._applied_plugins.dispose_all(conn.agent.registry)

        def _bye() -> None:
            import time
            time.sleep(0.2)
            os._exit(0)
        import threading
        threading.Thread(target=_bye, daemon=True).start()
        return {"status": "shutting down"}

    @app.get("/api/sessions")
    def api_sessions() -> dict[str, Any]:
        cur = os.getcwd()
        return {
            "project": list_project_sessions()[:20],
            "global": list_sessions()[:20],
            "current_workspace": cur,
            "groups": list_sessions_by_workspace(current_workspace=cur),
        }

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
    def api_cockpit(session_id: str = "") -> dict[str, Any]:
        sid = (session_id or "").strip()
        if sid and sid != "__live__":
            for conn in list(connections):
                if conn.store is not None and conn.store.id == sid:
                    return conn._cockpit_snapshot()
            from ..context import ConversationContext
            from ..plugins.builtin.cockpit import CockpitStore
            try:
                snap = load_session(sid)
            except Exception as exc:
                return {"enabled": True, "error": f"session not found: {exc}"}
            msgs = snap.get("messages") or []
            sys_prompt = snap.get("system_prompt") or ""
            tele = snap.get("telemetry") or {}
            limit = int(settings.get("context_window") or 200_000)
            ctx_obj = ConversationContext(config=Config(context_window=limit))
            ctx_obj.set_system_prompt(sys_prompt)
            ctx_obj.messages = list(msgs)
            ctx_info = ctx_obj.breakdown()
            ctx_info.setdefault("top_tools", [])
            ctx_info.setdefault("total_tool_calls", 0)
            calls = int(tele.get("calls") or 0)
            return {
                "enabled": True,
                "viewing_history": True,
                "session_id": sid,
                "session_title": snap.get("summary") or sid,
                "workspace": snap.get("workspace") or "",
                "mode": snap.get("permission_mode") or "ask",
                "context": ctx_info,
                "cache": {
                    "cumulative_hit_pct": 0,
                    "latest_uncached_in": int(tele.get("input_tokens") or 0),
                    "latest_out": int(tele.get("output_tokens") or 0),
                    "total_uncached_in": int(tele.get("input_tokens") or 0),
                    "total_cache_read": 0,
                    "total_out": int(tele.get("output_tokens") or 0),
                    "total_calls": calls,
                },
                "perf": {
                    "samples": calls,
                    "latest_latency_ms": None,
                    "latest_speed_tps": None,
                    "avg_latency_ms": None,
                    "p95_latency_ms": None,
                    "avg_speed_tps": None,
                    "peak_speed_tps": None,
                    "series": [],
                },
                "usage": CockpitStore().summary(),
            }
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
        if token:
            ws_tok = (
                websocket.cookies.get(DESKTOP_COOKIE)
                or websocket.headers.get("x-codelet-token")
                or websocket.query_params.get("desktop_token")
            )
            if not _token_ok(ws_tok):
                await websocket.close(code=1008)
                return
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
