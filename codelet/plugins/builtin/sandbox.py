"""Sandbox plugin: a separate `sandbox` tool that runs a shell command inside a
throwaway Docker container (the core `bash` tool is left untouched).

Enable in settings.json:
    {"plugins": {"enabled": ["sandbox"],
                 "config": {"sandbox": {"image": "python:3.12-slim", "network": false}}}}

Each command runs in `docker run --rm` with the current workspace bind-mounted at
/workspace (working dir), so it can't touch the host outside that folder, and (by
default) has no network. Real isolation via the container boundary. The model sees
both `bash` (host) and `sandbox` (isolated) and picks per command.

Caveats: needs Docker; each command is a fresh container (no cd/env persistence
between calls, ~1-2s startup); the first run pulls the image. For a persistent-
container or WSL/rootless backend, this is the seam to extend.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from ...plugins.base import PluginContext
from ...tools.base import Tool, ToolResult

DEFAULT_IMAGE = "python:3.12-slim"
TIMEOUT_S = 120


class SandboxTool(Tool):
    def __init__(self, image: str, network: bool) -> None:
        self._image = image
        self._network = network

    @property
    def name(self) -> str:
        return "sandbox"  # a separate tool; the core `bash` stays as-is

    @property
    def description(self) -> str:
        net = "with network" if self._network else "no network"
        return (
            f"Run a shell command inside an isolated Docker container "
            f"(image {self._image}, {net}, workspace mounted at /workspace). "
            "Like `bash` but sandboxed — prefer it for untrusted, experimental, or "
            "destructive commands. Each call is a fresh container: state does not "
            "persist between commands, so chain steps with '&&' in one call."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command to run."}},
            "required": ["command"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        command = params.get("command", "")
        if not command.strip():
            return ToolResult(output="Error: empty command.", is_error=True)
        if shutil.which("docker") is None:
            return ToolResult(output="Sandbox unavailable: docker not found on PATH.", is_error=True)

        workspace = os.getcwd()
        argv = ["docker", "run", "--rm", "-i"]
        if not self._network:
            argv += ["--network", "none"]
        argv += ["-v", f"{workspace}:/workspace", "-w", "/workspace", self._image,
                 "bash", "-lc", command]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return ToolResult(output=f"Sandboxed command timed out after {TIMEOUT_S}s.", is_error=True)
        except Exception as exc:
            return ToolResult(output=f"Sandbox error: {exc}", is_error=True)

        out = (proc.stdout or "") + (proc.stderr or "")
        out = out.strip() or "(no output)"
        return ToolResult(output=out, is_error=proc.returncode != 0)


class SandboxPlugin:
    name = "sandbox"

    def setup(self, ctx: PluginContext) -> None:
        image = str(ctx.config.get("image") or DEFAULT_IMAGE)
        network = bool(ctx.config.get("network", False))
        ctx.register_tool(SandboxTool(image, network))
        ctx.add_system_prompt_section(
            "A **`sandbox`** tool runs shell commands in an isolated Docker container "
            f"(image {image}, workspace at /workspace"
            f"{'' if network else ', no network'}) — use it instead of `bash` for "
            "untrusted or risky commands. Each call is a fresh container (no state "
            "carried over), so combine dependent steps with '&&'."
        )


PLUGIN = SandboxPlugin()
