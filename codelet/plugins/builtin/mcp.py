"""Built-in `mcp` bridge plugin -- connect external MCP servers (stdio / HTTP).

Zero extra dependencies beyond Python stdlib + `httpx` (already a core dep).
Configure servers in `.codelet/settings.json`:

    {
      "plugins": {
        "enabled": ["cockpit", "mcp"],
        "config": {
          "mcp": {
            "servers": {
              "local_demo": {
                "transport": "stdio",
                "command": ["python", "my_mcp_server.py"],
                "env": {"FOO": "bar"}
              },
              "remote_api": {
                "transport": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ..."}
              }
            }
          }
        }
      }
    }

Lifecycle:
  - On mount: connects to each configured server (`initialize` -> `notifications/initialized`
    -> `tools/list`), wraps every discovered tool as `mcp__<server>__<tool>` in `ToolRegistry`,
    and registers an `MCP` dynamic panel in the Web Inspector + `/mcp` slash command.
  - On unmount (`ctx.on_dispose`): cleanly terminates every stdio subprocess and closes HTTP
    clients so toggling `mcp` off leaves zero orphan processes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...tools.base import Tool, ToolResult
from ..base import PluginContext

MCP_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 15.0


def _safe_ident(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", (raw or "").strip()).strip("_")
    return s or "tool"


def _format_call_result(result: Any) -> ToolResult:
    """Translate an MCP `tools/call` JSON-RPC result into a codelet `ToolResult`."""
    if not isinstance(result, dict):
        return ToolResult(output=str(result))
    is_error = bool(result.get("isError", False))
    content = result.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            itype = item.get("type")
            if itype == "text":
                parts.append(str(item.get("text", "")))
            elif itype == "resource":
                res = item.get("resource") or {}
                parts.append(str(res.get("text") or res.get("uri") or json.dumps(res, ensure_ascii=False)))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return ToolResult(output="\n".join(p for p in parts if p) or "(empty)", is_error=is_error)
    return ToolResult(output=json.dumps(result, ensure_ascii=False, indent=2), is_error=is_error)


class MCPServerClient:
    """Abstract base for stdio / HTTP JSON-RPC 2.0 MCP server connections."""

    transport: str = "base"

    def __init__(self, name: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.name = name
        self.timeout = float(timeout)
        self._next_id = 0
        self._lock = threading.Lock()
        self.connected = False
        self.error: str = ""
        self.server_info: dict[str, Any] = {}
        self.tools: list[dict[str, Any]] = []

    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        raise NotImplementedError

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        pass

    def connect(self) -> list[dict[str, Any]]:
        init_res = self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "codelet", "version": "0.1.0"},
            },
        )
        if isinstance(init_res, dict):
            self.server_info = dict(init_res.get("serverInfo") or {})
        self.notify("notifications/initialized", {})
        listed = self.request("tools/list", {})
        tools = listed.get("tools", []) if isinstance(listed, dict) else []
        self.tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        self.connected = True
        self.error = ""
        return self.tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        res = self.request("tools/call", {"name": tool_name, "arguments": arguments})
        return _format_call_result(res)

    def close(self) -> None:
        self.connected = False


class StdioMCPClient(MCPServerClient):
    """Line-delimited JSON-RPC 2.0 over stdin/stdout of a local MCP subprocess."""

    transport = "stdio"

    def __init__(
        self,
        name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(name, timeout=timeout)
        self.command = list(command)
        self.env = dict(env or {})
        self.cwd = cwd
        self._proc: subprocess.Popen[str] | None = None

    def _ensure_proc(self) -> subprocess.Popen[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        merged_env = {**os.environ, **self.env}
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=merged_env,
            cwd=self.cwd,
        )
        return self._proc

    def _write_msg(self, payload: dict[str, Any]) -> None:
        proc = self._ensure_proc()
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def _read_response(self, expected_id: int) -> Any:
        proc = self._ensure_proc()
        assert proc.stdout is not None
        result_box: list[Any] = []
        err_box: list[Exception] = []

        def _reader() -> None:
            try:
                while True:
                    line = proc.stdout.readline()  # type: ignore[union-attr]
                    if not line:
                        raise EOFError(f"MCP stdio server '{self.name}' closed stdout")
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # skip stray log lines printed to stdout
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("id") == expected_id:
                        result_box.append(msg)
                        return
            except Exception as exc:
                err_box.append(exc)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=self.timeout)
        if t.is_alive():
            raise TimeoutError(f"MCP server '{self.name}' timed out after {self.timeout}s")
        if err_box:
            raise err_box[0]
        msg = result_box[0]
        if "error" in msg and msg["error"] is not None:
            err = msg["error"]
            detail = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"MCP error from '{self.name}': {detail}")
        return msg.get("result")

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            rid = self._alloc_id()
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                payload["params"] = params
            self._write_msg(payload)
            return self._read_response(rid)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            self._write_msg(payload)

    def close(self) -> None:
        super().close()
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


class HttpMCPClient(MCPServerClient):
    """Streamable HTTP / JSON-RPC 2.0 MCP client over httpx."""

    transport = "http"

    def __init__(
        self,
        name: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(name, timeout=timeout)
        self.url = url.strip()
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **(headers or {}),
        }
        self._session_id: str | None = None
        self._client = httpx.Client(timeout=self.timeout, follow_redirects=True)

    def _parse_http_body(self, resp: httpx.Response) -> dict[str, Any]:
        ctype = resp.headers.get("content-type", "").lower()
        if "text/event-stream" in ctype:
            # Parse SSE `data: {...}` lines and take the last JSON-RPC payload.
            last_obj: dict[str, Any] | None = None
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        obj = json.loads(raw)
                        if isinstance(obj, dict):
                            last_obj = obj
                    except json.JSONDecodeError:
                        continue
            if last_obj is None:
                raise ValueError("Empty SSE response from MCP server")
            return last_obj
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid JSON-RPC response from MCP server")
        return data

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            rid = self._alloc_id()
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                payload["params"] = params
            hdrs = dict(self.headers)
            if self._session_id:
                hdrs["Mcp-Session-Id"] = self._session_id
            resp = self._client.post(self.url, json=payload, headers=hdrs)
            resp.raise_for_status()
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self._session_id = sid
            msg = self._parse_http_body(resp)
            if "error" in msg and msg["error"] is not None:
                err = msg["error"]
                detail = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                raise RuntimeError(f"MCP error from '{self.name}': {detail}")
            return msg.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            hdrs = dict(self.headers)
            if self._session_id:
                hdrs["Mcp-Session-Id"] = self._session_id
            try:
                self._client.post(self.url, json=payload, headers=hdrs)
            except Exception:
                pass

    def close(self) -> None:
        super().close()
        try:
            self._client.close()
        except Exception:
            pass


class MCPProxyTool(Tool):
    """Bridges one remote MCP tool into codelet's native `Tool` interface."""

    read_only = False
    confirm_in_ask = True

    def __init__(self, client: MCPServerClient, spec: dict[str, Any]) -> None:
        self._client = client
        self._remote_name = str(spec["name"])
        self._tool_name = f"mcp__{_safe_ident(client.name)}__{_safe_ident(self._remote_name)}"
        raw_desc = str(spec.get("description") or f"MCP tool '{self._remote_name}' from server '{client.name}'").strip()
        self._description = f"[MCP:{client.name}] {raw_desc}"
        schema = spec.get("inputSchema") or spec.get("input_schema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        schema = dict(schema)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        self._schema = schema
        ann = spec.get("annotations") or {}
        if isinstance(ann, dict) and ann.get("readOnlyHint") is True:
            self.read_only = True
            self.confirm_in_ask = False

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._schema

    def preview_diff(self, params: dict[str, Any]) -> str | None:
        args_str = json.dumps(params or {}, ensure_ascii=False, indent=2)
        return f"MCP call → {self._client.name}/{self._remote_name}\n{args_str}"

    def execute(self, params: dict[str, Any]) -> ToolResult:
        try:
            return self._client.call_tool(self._remote_name, dict(params or {}))
        except Exception as exc:
            return ToolResult(output=f"MCP tool '{self._tool_name}' failed: {exc}", is_error=True)


@dataclass
class MCPRuntime:
    clients: dict[str, MCPServerClient] = field(default_factory=dict)
    registered_tools: list[str] = field(default_factory=list)

    def status_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name, cl in self.clients.items():
            rows.append({
                "name": name,
                "transport": cl.transport,
                "connected": cl.connected,
                "tools": [t["name"] for t in cl.tools],
                "error": cl.error,
                "server_info": cl.server_info,
            })
        return rows

    def close_all(self) -> None:
        for cl in self.clients.values():
            cl.close()
        self.clients.clear()


def _build_client(name: str, spec: dict[str, Any]) -> MCPServerClient:
    transport = str(spec.get("transport") or ("http" if spec.get("url") else "stdio")).strip().lower()
    timeout = float(spec.get("timeout", DEFAULT_TIMEOUT))
    if transport == "http":
        url = str(spec.get("url") or "").strip()
        if not url:
            raise ValueError(f"MCP server '{name}' (http) missing 'url'")
        return HttpMCPClient(name, url, headers=spec.get("headers"), timeout=timeout)
    cmd = spec.get("command")
    if isinstance(cmd, str):
        cmd = [cmd, *(spec.get("args") or [])]
    elif isinstance(cmd, list):
        cmd = [str(x) for x in cmd] + [str(x) for x in (spec.get("args") or [])]
    else:
        raise ValueError(f"MCP server '{name}' (stdio) missing 'command'")
    return StdioMCPClient(name, cmd, env=spec.get("env"), cwd=spec.get("cwd"), timeout=timeout)


class MCPPlugin:
    name = "mcp"
    description = "Bridge external MCP servers (stdio / HTTP) into native codelet tools (/mcp)."

    def setup(self, ctx: PluginContext) -> None:
        servers_cfg = ctx.config.get("servers") or {}
        runtime = MCPRuntime()
        ctx.provide("mcp", runtime)
        ctx.on_dispose(runtime.close_all)

        if isinstance(servers_cfg, dict):
            for srv_name, spec in servers_cfg.items():
                if not isinstance(spec, dict) or spec.get("enabled") is False:
                    continue
                try:
                    cl = _build_client(str(srv_name), spec)
                    runtime.clients[str(srv_name)] = cl
                    tools = cl.connect()
                    for tspec in tools:
                        proxy = MCPProxyTool(cl, tspec)
                        ctx.register_tool(proxy)
                        runtime.registered_tools.append(proxy.name)
                except Exception as exc:
                    cl_err = runtime.clients.get(str(srv_name))
                    if cl_err is not None:
                        cl_err.error = str(exc)
                    else:
                        dummy = MCPServerClient(str(srv_name))
                        dummy.error = str(exc)
                        runtime.clients[str(srv_name)] = dummy

        ctx.register_skill(
            name="mcp-usage",
            description="Discover and invoke bridged external MCP tools (`mcp__<server>__<tool>`) safely",
            category="integration",
            triggers=("mcp", "external tool", "connector", "server"),
            body=(
                "# MCP Tool Usage Guidelines\n\n"
                "1. External MCP tools are namespaced as `mcp__<server>__<tool>`.\n"
                "2. Check tool input schemas carefully before calling; prefer read-only MCP calls first.\n"
                "3. Run `/mcp` if a server looks disconnected or needs inspection."
            ),
        )

        def _cmd_mcp(args: str) -> str:
            rows = runtime.status_rows()
            if not rows:
                return "[mcp] no servers configured (add `plugins.config.mcp.servers` in .codelet/settings.json)"
            lines = [f"[mcp] {len(rows)} server(s), {len(runtime.registered_tools)} tool(s):"]
            for r in rows:
                st = "connected" if r["connected"] else f"error: {r['error']}"
                lines.append(f"  - {r['name']} ({r['transport']}): {st} · {len(r['tools'])} tools")
            return "\n".join(lines)

        ctx.register_command("mcp", _cmd_mcp)

        def _render_panel() -> dict[str, Any]:
            rows = runtime.status_rows()
            if not rows:
                return {
                    "sections": [{
                        "title": "MCP Servers",
                        "items": [{
                            "label": "No servers configured",
                            "value": "Add `plugins.config.mcp.servers` in `.codelet/settings.json`",
                            "badge": "idle",
                        }],
                    }]
                }
            sections = []
            for r in rows:
                items = [
                    {"label": "Transport", "value": r["transport"]},
                    {"label": "Status", "value": "Connected" if r["connected"] else (r["error"] or "Disconnected"),
                     "badge": "ok" if r["connected"] else "err"},
                ]
                for tname in r["tools"]:
                    items.append({"label": tname, "value": f"mcp__{_safe_ident(r['name'])}__{_safe_ident(tname)}", "mono": True})
                sections.append({"title": r["name"], "items": items})
            return {"sections": sections}

        ctx.register_panel(
            "mcp-servers",
            "MCP",
            _render_panel,
            description="Connected external MCP servers and bridged tools",
        )


PLUGIN = MCPPlugin()
