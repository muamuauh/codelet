"""RAG plugin: retrieval over the workspace's own files, no external services.

Enable in settings.json:
    {"plugins": {"enabled": ["rag"],
                 "config": {"rag": {"inject": false, "top_k": 3}}}}

Contributes a `search_docs(query, k)` tool that returns the most relevant file
chunks (BM25 over line-blocks of the workspace's code/docs), a `/rag` command that
(re)builds the index and reports stats, and — if `inject` is set — a prompt
middleware that prepends the top hits to each message. Pure-Python lexical
retrieval (no embedding API); the index rebuilds when the workspace changes.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from ...plugins.base import PluginContext
from ...tools.base import Tool, ToolResult

DEFAULT_GLOBS = [
    "**/*.py", "**/*.md", "**/*.txt", "**/*.rst", "**/*.js", "**/*.ts", "**/*.tsx",
    "**/*.json", "**/*.yaml", "**/*.yml", "**/*.toml", "**/*.cfg", "**/*.ini",
]
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".codelet", ".venv", "venv", "dist",
    "build", ".mypy_cache", ".pytest_cache", ".idea", ".vscode", "site-packages",
}
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


class _Chunk:
    __slots__ = ("file", "start", "text", "tf", "length")

    def __init__(self, file: str, start: int, text: str) -> None:
        self.file = file
        self.start = start
        self.text = text
        self.tf = Counter(_tokenize(text))
        self.length = sum(self.tf.values()) or 1


class RagIndex:
    def __init__(self, root: str, globs: list[str], chunk_lines: int,
                 max_files: int, max_bytes: int) -> None:
        self.root = Path(root)
        self.globs = globs
        self.chunk_lines = max(5, chunk_lines)
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.chunks: list[_Chunk] = []
        self.df: Counter = Counter()
        self.avglen = 1.0
        self.n_files = 0
        self._build()

    def _files(self):
        seen: set[Path] = set()
        for g in self.globs:
            for p in self.root.glob(g):
                if p in seen or not p.is_file():
                    continue
                if any(part in SKIP_DIRS for part in p.parts):
                    continue
                seen.add(p)
                yield p

    def _build(self) -> None:
        files = []
        for p in self._files():
            files.append(p)
            if len(files) >= self.max_files:
                break
        for p in files:
            try:
                if p.stat().st_size > self.max_bytes:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = text.splitlines()
            try:
                rel = str(p.relative_to(self.root))
            except ValueError:
                rel = str(p)
            for i in range(0, len(lines), self.chunk_lines):
                block = "\n".join(lines[i:i + self.chunk_lines]).strip()
                if block:
                    self.chunks.append(_Chunk(rel, i + 1, block))
        for c in self.chunks:
            for term in c.tf:
                self.df[term] += 1
        if self.chunks:
            self.avglen = sum(c.length for c in self.chunks) / len(self.chunks)
        self.n_files = len(files)

    def search(self, query: str, k: int = 5) -> list[_Chunk]:
        terms = _tokenize(query)
        if not terms or not self.chunks:
            return []
        n, k1, b = len(self.chunks), 1.5, 0.75
        scored: list[tuple[float, _Chunk]] = []
        for c in self.chunks:
            score = 0.0
            for term in terms:
                tf = c.tf.get(term, 0)
                if not tf:
                    continue
                df = self.df.get(term, 1)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * c.length / self.avglen))
            if score > 0:
                scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:k]]


class SearchDocsTool(Tool):
    def __init__(self, plugin: "RagPlugin") -> None:
        self._plugin = plugin

    @property
    def name(self) -> str:
        return "search_docs"

    @property
    def description(self) -> str:
        return ("Search the workspace's code and docs for text relevant to a query "
                "(BM25 over file chunks). Returns the top matching snippets with "
                "file:line. Use it to locate where something is defined or discussed "
                "before reading whole files.")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for."},
                "k": {"type": "integer", "description": "Max snippets (default 5)."},
            },
            "required": ["query"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        query = (params.get("query") or "").strip()
        if not query:
            return ToolResult(output="Error: 'query' is required.", is_error=True)
        k = int(params.get("k") or 5)
        hits = self._plugin.get_index().search(query, k)
        if not hits:
            return ToolResult(output="No matches in the workspace index.")
        return ToolResult(output="\n\n---\n".join(f"{c.file}:{c.start}\n{c.text}" for c in hits))


class RagPlugin:
    name = "rag"

    def __init__(self) -> None:
        self._index: RagIndex | None = None
        self._index_cwd: str | None = None
        self._cfg: dict[str, Any] = {}

    def setup(self, ctx: PluginContext) -> None:
        self._cfg = ctx.config or {}
        ctx.register_tool(SearchDocsTool(self))
        ctx.register_command("rag", self._cmd)
        ctx.add_system_prompt_section(
            "A `search_docs` tool is available: BM25 retrieval over this workspace's "
            "files. Prefer it to locate relevant code/docs before reading files in full."
        )
        if bool(self._cfg.get("inject", False)):
            ctx.on_user_prompt(self._inject)

    def get_index(self) -> RagIndex:
        cwd = os.getcwd()
        if self._index is None or self._index_cwd != cwd:
            self._index = RagIndex(
                cwd,
                list(self._cfg.get("globs") or DEFAULT_GLOBS),
                int(self._cfg.get("chunk_lines") or 40),
                int(self._cfg.get("max_files") or 800),
                int(self._cfg.get("max_bytes") or 200_000),
            )
            self._index_cwd = cwd
        return self._index

    def _cmd(self, args: str) -> str:
        self._index = None  # force rebuild
        idx = self.get_index()
        return f"[rag] indexed {len(idx.chunks)} chunks from {idx.n_files} files under {idx.root}"

    def _inject(self, prompt: str) -> str:
        try:
            hits = self.get_index().search(prompt, int(self._cfg.get("top_k") or 3))
        except Exception:
            return prompt
        if not hits:
            return prompt
        block = "\n\n".join(f"# {c.file}:{c.start}\n{c.text}" for c in hits)
        return f"{prompt}\n\n<retrieved_context>\n{block}\n</retrieved_context>"


PLUGIN = RagPlugin()
