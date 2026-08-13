"""WebFetch tool -- fetch a URL and return readable text.

Async-native (overrides aexecute, no execute), so it doesn't waste a worker
thread waiting on httpx. Strips scripts/styles, collapses whitespace, and
truncates to MAX_OUTPUT_CHARS.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

import httpx

from .base import Tool, ToolResult

MAX_OUTPUT_CHARS = 30_000
DEFAULT_TIMEOUT_SECONDS = 20.0
USER_AGENT = "codelet/0.1 (+https://github.com/gjq00/my-codelet)"


class _TextExtractor(HTMLParser):
    """Pulls visible text out of HTML, dropping script/style/noscript."""

    SKIP_TAGS = {"script", "style", "noscript", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag.lower() in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._buf.append(data)

    def text(self) -> str:
        joined = "".join(self._buf)
        # Collapse whitespace runs (newlines preserved as single newlines).
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()


class WebFetchTool(Tool):
    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "Fetch a URL and return the page text (HTML stripped to readable "
            "content; JSON / plain text returned as-is). Use for reading docs, "
            "API reference pages, or any web resource. Output capped at "
            f"~{MAX_OUTPUT_CHARS} characters."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute URL (http/https)."},
                "timeout_seconds": {
                    "type": "number",
                    "description": f"Request timeout. Default {DEFAULT_TIMEOUT_SECONDS}s.",
                },
            },
            "required": ["url"],
        }

    def check_permissions(self, params: dict[str, Any]) -> str | None:
        url = (params.get("url") or "").strip()
        if not url:
            return "url is required"
        if not (url.startswith("http://") or url.startswith("https://")):
            return "url must start with http:// or https://"
        return None

    async def aexecute(self, params: dict[str, Any]) -> ToolResult:
        url = params["url"].strip()
        timeout = float(params.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                response = await client.get(url)
        except httpx.TimeoutException:
            return ToolResult(output=f"Error: timeout after {timeout}s fetching {url}", is_error=True)
        except httpx.HTTPError as exc:
            return ToolResult(output=f"Error: {type(exc).__name__}: {exc}", is_error=True)

        ct = response.headers.get("content-type", "").lower()
        body = response.text

        if "html" in ct:
            extractor = _TextExtractor()
            try:
                extractor.feed(body)
            except Exception:
                pass
            body = extractor.text() or body

        if len(body) > MAX_OUTPUT_CHARS:
            body = body[:MAX_OUTPUT_CHARS] + f"\n... (truncated; full size {len(body)} chars)"

        header = f"[{response.status_code}] {url}\nContent-Type: {ct or '?'}\n\n"
        is_error = response.status_code >= 400
        return ToolResult(output=header + body, is_error=is_error)
