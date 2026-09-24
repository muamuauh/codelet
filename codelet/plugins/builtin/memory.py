"""Memory plugin: a few typed facts that the NEXT session should know.

Enable in settings.json:
    {"plugins": {"enabled": ["memory"],
                 "config": {"memory": {"dir": ".codelet/memory",
                                       "user_dir": "~/.codelet/memory"}}}}

codelet has three ways of "remembering", which are easy to conflate:
  - session history (persistence/session.py) -- the whole transcript, for /resume;
  - the compaction summary (context.py) -- a lossy digest *within* one session;
  - memory (this plugin) -- short, typed facts carried *across* sessions.

One markdown file per memory, with a small front matter:

    ---
    key: test-file-naming
    type: feedback
    description: Tests are named check_*.py in this repo, not test_*.py
    source: user correction
    updated: 2026-09-24
    ---
    The body: the fact, then why it holds and how to apply it.

Choices, and why:
  - The index is derived from the files every time, never stored, so it cannot
    drift from them. Editing or deleting a file by hand is a supported way to
    change what the agent remembers.
  - Only the index goes into the system prompt, one line per entry (like skills);
    the body is one `view` away. `description` must state the fact itself, not a
    title, because it is the only part a new session sees without asking.
  - The index is read when the session starts and not rebuilt on every write:
    the system prompt stays stable, and within the session the model already has
    what it wrote in its own context.
  - Writes are upserts by key, so a correction replaces the stale entry instead of
    piling up a near-duplicate. In ASK mode every write and delete goes through the
    same diff approval as write_file.
  - Content that looks like a credential is refused: memory files are plain text,
    and a project's memory directory may end up committed.
  - `type: user` (who the user is, how they like to work) defaults to the user
    directory, shared by all projects; everything else stays with the project.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ...plugins.base import PluginContext
from ...tools.base import Tool, ToolResult

DEFAULT_DIR = ".codelet/memory"
DEFAULT_USER_DIR = "~/.codelet/memory"
TYPES = ("user", "feedback", "project", "reference")
SCOPES = ("project", "user")
MAX_INDEX = 40          # entries listed in the system prompt
MAX_DESCRIPTION = 150   # characters; longer is refused, not truncated

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}"                      # OpenAI / Anthropic style keys
    r"|AKIA[0-9A-Z]{16}"                          # AWS access key id
    r"|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|(?i:api[_-]?key|password|passwd|secret|token)\s*[:=]\s*['\"]?[^\s'\"]{8,}"
)

PROMPT_HEAD = (
    "## Memory (persists across sessions)\n\n"
    "Facts saved in earlier sessions. Only this index is loaded; call `memory` with "
    "action \"view\" and the key to read an entry's details. Entries can be out of "
    "date -- check one against the code before acting on anything that matters."
)
PROMPT_RULES = (
    "Save a memory (`memory`, action \"write\") when the user corrects you or states a "
    "preference, convention or constraint that will matter in future sessions -- at "
    "the moment it happens, before carrying out the fix it asks for -- or when "
    "you learn a non-obvious fact about this project that the code, docs and git "
    "history do not already record. Do not save what only matters in this session, "
    "and never save secrets. One fact per key; to change a fact, write the same key "
    "again rather than adding a near-duplicate. Make `description` state the fact "
    "itself (\"Tests are named check_*.py here\"), not a title (\"Test naming\") -- it "
    "is the only part a future session sees up front."
)


@dataclass
class Memory:
    key: str
    type: str
    description: str
    body: str
    source: str
    updated: str
    scope: str
    path: Path

    def index_line(self) -> str:
        where = "" if self.scope == "project" else f"{self.scope}/"
        when = f" (updated {self.updated})" if self.updated else ""
        return f"- [{where}{self.type}] {self.key}: {self.description}{when}"


def render(m: Memory) -> str:
    return (f"---\nkey: {m.key}\ntype: {m.type}\ndescription: {m.description}\n"
            f"source: {m.source}\nupdated: {m.updated}\n---\n{m.body.strip()}\n")


def parse(path: Path, scope: str) -> Memory | None:
    """Front matter + body. A hand-written file without front matter still counts:
    its stem is the key and its first line the description."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        head, sep, rest = text[3:].partition("\n---")
        if sep:
            for line in head.splitlines():
                k, colon, v = line.partition(":")
                if colon:
                    meta[k.strip()] = v.strip()
            body = rest.lstrip("\n")
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return Memory(
        key=meta.get("key") or path.stem,
        type=meta.get("type") if meta.get("type") in TYPES else "project",
        description=meta.get("description") or first[:MAX_DESCRIPTION] or "(no description)",
        body=body,
        source=meta.get("source", ""),
        updated=meta.get("updated", ""),
        scope=scope,
        path=path,
    )


class MemoryStore:
    def __init__(self, dirs: dict[str, Path]) -> None:
        self.dirs = dirs        # scope -> directory

    def all(self) -> list[Memory]:
        found: list[Memory] = []
        for scope, base in self.dirs.items():
            if base.is_dir():
                for f in sorted(base.glob("*.md")):
                    m = parse(f, scope)
                    if m is not None:
                        found.append(m)
        # Newest first: when the index is capped, recent facts are the ones kept.
        return sorted(found, key=lambda m: m.updated, reverse=True)

    def find(self, key: str, scope: str | None = None) -> Memory | None:
        for m in self.all():
            if m.key == key and (scope is None or m.scope == scope):
                return m
        return None

    def path_for(self, key: str, scope: str) -> Path:
        return self.dirs[scope] / f"{key}.md"

    def index(self, limit: int = MAX_INDEX) -> str:
        items = self.all()
        lines = [m.index_line() for m in items[:limit]]
        if len(items) > limit:
            lines.append(f"- (... {len(items) - limit} older entries; `memory` view lists all)")
        return "\n".join(lines)


class MemoryTool(Tool):
    confirm_in_ask = True   # ASK mode: writes and deletes go through diff approval

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return (
            "Long-term memory across sessions. When the user has just corrected you or "
            "stated a rule for this project (\"we don't use X here\", \"always put Y in "
            "Z\"), write it down FIRST, then continue with the fix -- otherwise the next "
            "session repeats the mistake. action=\"view\": with no key, list every entry; "
            "with a key, show that entry in full. action=\"write\": create or replace the "
            "entry for `key` (needs type, description, content). action=\"delete\": "
            "remove `key`."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["view", "write", "delete"]},
                "key": {"type": "string",
                        "description": "lowercase-hyphenated slug, e.g. 'test-file-naming'."},
                "type": {"type": "string", "enum": list(TYPES),
                         "description": "user: who the user is / how they work. feedback: a "
                         "correction or preference about how to work. project: a fact about "
                         "this codebase. reference: where something lives outside the repo."},
                "description": {"type": "string",
                                "description": f"One line (<= {MAX_DESCRIPTION} chars) stating "
                                "the fact itself -- the only part shown in the index."},
                "content": {"type": "string",
                            "description": "The fact, then why it holds and how to apply it."},
                "source": {"type": "string",
                           "description": "Where it came from, e.g. 'user correction', "
                           "'observed in setup.cfg'."},
                "scope": {"type": "string", "enum": list(SCOPES),
                          "description": "Default: user for type=user, else project."},
            },
            "required": ["action"],
        }

    # ---- validation shared by preview + execute ----

    def _planned(self, params: dict[str, Any]) -> tuple[Path, str | None, str | None] | str:
        """(path, old text, new text) the action would produce, or an error string."""
        action = params.get("action")
        key = str(params.get("key") or "").strip()
        if action not in ("write", "delete"):
            return f"Error: unknown action {action!r}."
        if not _KEY_RE.match(key):
            return "Error: key must be a lowercase-hyphenated slug like 'test-file-naming'."
        mtype = str(params.get("type") or "")
        existing = None if params.get("scope") else self._store.find(key)
        # Upsert by key: a key that already exists keeps its scope, so a correction
        # never leaves the stale entry behind in the other directory.
        scope = str(params.get("scope") or (existing.scope if existing else
                                            ("user" if mtype == "user" else "project")))
        if scope not in SCOPES:
            return f"Error: scope must be one of {', '.join(SCOPES)}."
        path = self._store.path_for(key, scope)
        old = path.read_text(encoding="utf-8") if path.is_file() else None
        if action == "delete":
            return (path, old, None) if old is not None else f"Error: no memory '{key}' in {scope}."

        description = " ".join(str(params.get("description") or "").split())
        content = str(params.get("content") or "").strip()
        if mtype not in TYPES:
            return f"Error: type must be one of {', '.join(TYPES)}."
        if not description or not content:
            return "Error: write needs both a one-line description and the content."
        if len(description) > MAX_DESCRIPTION:
            return (f"Error: description is {len(description)} chars; keep it under "
                    f"{MAX_DESCRIPTION} and put detail in content.")
        if _SECRET_RE.search(description + "\n" + content):
            return "Error: this looks like a credential. Memory files are plain text; never store secrets."
        new = render(Memory(key, mtype, description, content, str(params.get("source") or "").strip(),
                            date.today().isoformat(), scope, path))
        return path, old, new

    def preview_diff(self, params: dict[str, Any]) -> str | None:
        planned = self._planned(params) if params.get("action") != "view" else None
        if not isinstance(planned, tuple):
            return None
        path, old, new = planned
        diff = difflib.unified_diff((old or "").splitlines(keepends=True),
                                    (new or "").splitlines(keepends=True),
                                    fromfile=str(path) if old is not None else "/dev/null",
                                    tofile=str(path) if new is not None else "/dev/null")
        return "".join(diff) or None

    def execute(self, params: dict[str, Any]) -> ToolResult:
        if params.get("action") == "view":
            return self._view(str(params.get("key") or "").strip())
        planned = self._planned(params)
        if isinstance(planned, str):
            return ToolResult(output=planned, is_error=True)
        path, old, new = planned
        if new is None:
            path.unlink()
            return ToolResult(output=f"Deleted memory {path.stem}.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
        verb = "Updated" if old is not None else "Saved"
        return ToolResult(output=f"{verb} memory {path.stem} ({path}). Future sessions will see "
                                 "its description in the index.")

    def _view(self, key: str) -> ToolResult:
        if not key:
            index = self._store.index(limit=10_000)
            return ToolResult(output=index or "(no memories yet)")
        m = self._store.find(key)
        if m is None:
            return ToolResult(output=f"No memory '{key}'. Call view with no key to list them.",
                              is_error=True)
        return ToolResult(output=render(m))


class MemoryPlugin:
    name = "memory"

    def setup(self, ctx: PluginContext) -> None:
        store = MemoryStore({
            "project": Path(str(ctx.config.get("dir") or DEFAULT_DIR)).expanduser(),
            "user": Path(str(ctx.config.get("user_dir") or DEFAULT_USER_DIR)).expanduser(),
        })
        ctx.register_tool(MemoryTool(store))
        index = store.index()
        ctx.add_system_prompt_section(
            f"{PROMPT_HEAD}\n\n{index or '(no memories yet)'}\n\n{PROMPT_RULES}")
        ctx.register_command("memory", lambda args: store.index(limit=10_000) or "[memory] none yet")


PLUGIN = MemoryPlugin()
