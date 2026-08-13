"""Task tool -- spawns a SubAgent.

The model invokes this to delegate a focused job to a subagent that runs in
an isolated context. Multiple Task calls in one assistant turn fan out
through the parent's existing parallel dispatcher (see agent_loop._dispatch_parallel).

Bound at agent-loop construction time because it needs the parent's config,
registry, client, depth, and skill index.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..subagent.runner import MAX_SUBAGENT_DEPTH, SubAgentSession, SubAgentSpec
from .base import Tool, ToolResult

if TYPE_CHECKING:
    from ..agent_loop import AgentLoop


class TaskTool(Tool):
    def __init__(self, parent: "AgentLoop") -> None:
        # Store the parent reference; we read its current depth + registry
        # at execute time so depth is always fresh.
        self._parent = parent

    @property
    def name(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        return (
            "Spawn a SubAgent to handle a focused task in an isolated context. "
            "Multiple Task calls in one turn run in parallel. The subagent "
            "returns a single short summary string. Use this for: research "
            "across many files, repetitive analysis you'd otherwise loop, or "
            "any subtask whose intermediate state would clutter the main "
            f"context. Recursion is capped at depth {MAX_SUBAGENT_DEPTH}."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short label for this subagent (3-7 words).",
                },
                "prompt": {
                    "type": "string",
                    "description": "Self-contained task description; the subagent has no parent context.",
                },
                "agent_type": {
                    "type": "string",
                    "description": "Optional role hint (e.g. 'researcher', 'reviewer'). Default: general.",
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional whitelist of tool names; defaults to all parent tools.",
                },
            },
            "required": ["description", "prompt"],
        }

    async def aexecute(self, params: dict[str, Any]) -> ToolResult:
        description = (params.get("description") or "").strip() or "subagent"
        prompt = (params.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(output="Error: 'prompt' is required.", is_error=True)

        agent_type = (params.get("agent_type") or "general").strip() or "general"
        allowed_tools_raw = params.get("allowed_tools")
        allowed_tools = (
            tuple(str(t) for t in allowed_tools_raw) if isinstance(allowed_tools_raw, list) else None
        )

        session = SubAgentSession(
            parent_config=self._parent.config,
            parent_registry=self._parent.registry,
            client=self._parent.client,
            parent_depth=self._parent.context.depth,
            skill_index=self._parent.skill_index,
            console=self._parent.console,
            hook_runner=self._parent.hooks,
            telemetry=self._parent.telemetry,
        )
        spec = SubAgentSpec(
            description=description,
            prompt=prompt,
            agent_type=agent_type,
            allowed_tools=allowed_tools,
        )
        result = await session.run(spec)

        if result.depth_capped:
            return ToolResult(output=result.summary, is_error=True)

        meta = (
            f"\n\n---\n[subagent metadata] description={description!r} "
            f"agent_type={agent_type} turns={result.turns} "
            f"tools_used={result.tools_used} truncated={result.truncated}"
        )
        return ToolResult(output=result.summary + meta)
