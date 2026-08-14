"""Session persistence -- JSON snapshots of ConversationContext.

Sessions live at `~/.codelet/sessions/{id}.json`. Each save fully
overwrites the file (atomic via tmp + rename) so an interrupted save can
never produce a half-written corrupted snapshot.

Snapshot shape:
    {
      "id": "20260503-153045-abcd",
      "created_at": "2026-05-03T15:30:45.123456",
      "updated_at": "2026-05-03T15:42:11.987654",
      "model": "claude-sonnet-4-5",
      "provider": "anthropic",
      "profile": "anthropic",
      "summary": "find all TODOs",   // first user message, truncated (used as a title)
      "system_prompt": "...",
      "messages": [...],            // ConversationContext.messages verbatim
      "compactions": 2,
      "depth": 0,
      "todos": [...],               // serialized TodoStore items (optional)
      "telemetry": {...}            // optional summary, see _telemetry_dump
    }

The CLI exposes `/resume <id>` which calls `load_session` and reconstructs the
context. Tool registries / hooks / skills are NOT serialized -- those are
re-derived from the current settings.json on resume.

Project registry: snapshots live in one global folder, but a `SessionStore`
given a `project_dir` also appends a lightweight entry (id, title, updated_at,
model) to `{project_dir}/sessions.json`. That lets `/sessions` show "sessions
touched from this working directory" separately from the global list.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..agent_loop import AgentLoop


SESSION_DIR = Path.home() / ".codelet" / "sessions"
PROJECT_REGISTRY_NAME = "sessions.json"


class SessionStore:
    """Owns one session's id and writes snapshots through `record(...)`.

    AgentLoop holds at most one. Calling `record(agent)` after each user
    turn keeps the on-disk snapshot fresh. Construction is cheap (no IO);
    the directory is created lazily on first save.

    If `project_dir` is set, each save also registers this session in
    `{project_dir}/sessions.json` so `/sessions` can list the sessions that
    belong to this working directory. Left None (the default used by tests and
    one-shot saves) the registry is never touched.
    """

    def __init__(
        self,
        session_id: str | None = None,
        base_dir: Path | None = None,
        project_dir: Path | None = None,
    ) -> None:
        self.id = session_id or _new_session_id()
        self.base_dir = base_dir or SESSION_DIR
        self.project_dir = project_dir
        self.created_at = _utcnow_iso()

    @property
    def path(self) -> Path:
        return self.base_dir / f"{self.id}.json"

    def record(self, agent: "AgentLoop") -> Path:
        """Atomically write a snapshot of `agent` to disk."""
        snapshot = _serialize(agent, self.id, self.created_at)
        path = _write_atomic(self.path, snapshot)
        if self.project_dir is not None:
            # Best-effort: a broken registry must never sink the real save.
            try:
                record_project_session(
                    self.project_dir,
                    session_id=self.id,
                    title=snapshot.get("summary", ""),
                    updated_at=snapshot["updated_at"],
                    model=snapshot.get("model"),
                    provider=snapshot.get("provider"),
                    message_count=len(snapshot.get("messages") or []),
                )
            except Exception:
                pass
        return path


def save_session(agent: "AgentLoop", store: SessionStore | None = None) -> SessionStore:
    """One-shot save: build a fresh store if needed, write the snapshot, return it."""
    store = store or SessionStore()
    store.record(agent)
    return store


def load_session(session_id: str, base_dir: Path | None = None) -> dict[str, Any]:
    """Read a snapshot dict from disk. Raises FileNotFoundError if absent."""
    base_dir = base_dir or SESSION_DIR
    path = base_dir / f"{session_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def list_sessions(base_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return summaries (id, model, created_at, updated_at, message_count)
    for every snapshot, sorted newest-first by updated_at.
    """
    base_dir = base_dir or SESSION_DIR
    if not base_dir.is_dir():
        return []

    out: list[dict[str, Any]] = []
    for path in base_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "id": data.get("id", path.stem),
            "model": data.get("model"),
            "provider": data.get("provider"),
            "profile": data.get("profile"),
            "summary": data.get("summary") or "",
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "message_count": len(data.get("messages") or []),
        })
    out.sort(key=lambda d: d.get("updated_at") or "", reverse=True)
    return out


def rename_session(session_id: str, title: str, *, base_dir: Path | None = None,
                   project_dir: Path | None = None) -> None:
    """Retitle a session: update the snapshot's summary + the project registry."""
    base_dir = base_dir or SESSION_DIR
    path = base_dir / f"{session_id}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["summary"] = title
            _write_atomic(path, data)
        except (OSError, json.JSONDecodeError):
            pass
    reg = _read_registry(project_dir)
    if isinstance(reg.get(session_id), dict):
        reg[session_id]["title"] = title
        _write_atomic(_registry_path(project_dir), reg)


def delete_session(session_id: str, *, base_dir: Path | None = None,
                   project_dir: Path | None = None) -> None:
    """Remove a session snapshot and its project-registry entry."""
    base_dir = base_dir or SESSION_DIR
    try:
        (base_dir / f"{session_id}.json").unlink()
    except OSError:
        pass
    reg = _read_registry(project_dir)
    if session_id in reg:
        del reg[session_id]
        _write_atomic(_registry_path(project_dir), reg)


# ---------- project-local session registry ----------

def _project_dir_or_cwd(project_dir: Path | None) -> Path:
    return Path(project_dir) if project_dir is not None else (Path.cwd() / ".codelet")


def _registry_path(project_dir: Path | None) -> Path:
    return _project_dir_or_cwd(project_dir) / PROJECT_REGISTRY_NAME


def _read_registry(project_dir: Path | None) -> dict[str, Any]:
    try:
        data = json.loads(_registry_path(project_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def record_project_session(
    project_dir: Path,
    *,
    session_id: str,
    title: str,
    updated_at: str,
    model: str | None = None,
    provider: str | None = None,
    message_count: int = 0,
) -> Path:
    """Upsert this session's entry into `{project_dir}/sessions.json`."""
    reg = _read_registry(project_dir)
    reg[session_id] = {
        "title": title,
        "updated_at": updated_at,
        "model": model,
        "provider": provider,
        "message_count": message_count,
    }
    return _write_atomic(_registry_path(project_dir), reg)


def list_project_sessions(project_dir: Path | None = None) -> list[dict[str, Any]]:
    """Sessions touched from this working directory, newest-first. Each dict
    carries its id plus the registered title/updated_at/model/provider/count."""
    reg = _read_registry(project_dir)
    out = [{"id": sid, **entry} for sid, entry in reg.items() if isinstance(entry, dict)]
    out.sort(key=lambda d: d.get("updated_at") or "", reverse=True)
    return out


# ---------- serialization ----------

def _serialize(agent: "AgentLoop", session_id: str, created_at: str) -> dict[str, Any]:
    cfg = agent.config
    todos: list[dict[str, Any]] = []
    if getattr(agent, "todo_store", None) is not None:
        for t in agent.todo_store.todos:
            todos.append(asdict(t))

    return {
        "id": session_id,
        "created_at": created_at,
        "updated_at": _utcnow_iso(),
        "model": cfg.model,
        "provider": cfg.provider.value,
        "profile": cfg.profile_name,
        "permission_mode": cfg.permission_mode.value,
        "summary": _summarize(agent.context.messages),
        "system_prompt": agent.context.system_prompt,
        "messages": agent.context.messages,
        "compactions": agent.context.compactions,
        "depth": agent.context.depth,
        "todos": todos,
        "telemetry": _telemetry_dump(agent),
    }


def _telemetry_dump(agent: "AgentLoop") -> dict[str, Any]:
    tele = getattr(agent, "telemetry", None)
    if tele is None:
        return {}
    cum = tele.cumulative
    return {
        "input_tokens": cum.input_tokens,
        "output_tokens": cum.output_tokens,
        "cost_usd": cum.cost_usd,
        "calls": len(tele.turns),
    }


def restore_into(agent: "AgentLoop", snapshot: dict[str, Any]) -> None:
    """Apply a loaded snapshot to an AgentLoop in place.

    Only context state is restored. Tool registry, hooks, and skills come
    from the *current* config (so resuming with a tweaked settings.json
    picks up the change), not the snapshot.
    """
    agent.context.messages = list(snapshot.get("messages") or [])
    agent.context.set_system_prompt(snapshot.get("system_prompt") or "")
    agent.context.compactions = int(snapshot.get("compactions") or 0)
    agent.context.depth = int(snapshot.get("depth") or 0)

    # Best-effort todo restore: skip if shape doesn't match so a stale snapshot
    # never blocks resume.
    if hasattr(agent, "todo_store"):
        from ..tools.todo_write import Todo
        agent.todo_store.todos = []
        for entry in snapshot.get("todos") or []:
            try:
                agent.todo_store.todos.append(Todo(
                    content=entry["content"],
                    status=entry.get("status", "pending"),
                    active_form=entry.get("active_form", ""),
                ))
            except (KeyError, TypeError):
                continue


# ---------- helpers ----------

def _summarize(messages: list[Any], limit: int = 80) -> str:
    """Title for a session = its first user message, whitespace-collapsed and
    truncated. Cheap (no LLM) and stable, which is all the listing needs."""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _content_text(msg.get("content"))
        if text:
            text = " ".join(text.split())
            return text if len(text) <= limit else text[: limit - 1] + "…"
    return ""


def _content_text(content: Any) -> str:
    """Plain text from a message's content (str, or Anthropic block list).
    Skips non-text blocks (tool_use / tool_result) so a tool-only first turn
    doesn't yield a junk title."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return " ".join(p for p in parts if p)
    return ""


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _new_session_id() -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def _write_atomic(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    # Write to a sibling tmp file then rename so a kill mid-write can't leave
    # a half-baked JSON file behind.
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path
