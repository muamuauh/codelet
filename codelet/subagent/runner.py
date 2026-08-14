"""SubAgent runner.

A subagent is a fresh AgentLoop with:
  - a separate ConversationContext (parent's history does NOT leak in)
  - a stripped system prompt (subagent guidance + skill index, no parent CLAUDE.md)
  - shared tools / LLM client / skill index from the parent
  - depth = parent.depth + 1, hard-capped at MAX_SUBAGENT_DEPTH

Result protocol:
  The final assistant text is returned as a single summary string, truncated
  to MAX_SUMMARY_CHARS. Per-subagent turn cap is half the parent's max_turns
  (or PER_SUBAGENT_TURN_CAP, whichever is smaller) -- the spawning side can
  override.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from rich.console import Console

from ..config import Config
from ..context import ConversationContext
from ..hooks.runner import HookRunner
from ..llm.base import LLMClient
from ..skills.loader import SkillIndex
from ..telemetry import Telemetry
from ..tools.base import ToolRegistry

if TYPE_CHECKING:  # avoid circular at runtime
    from ..agent_loop import AgentLoop


MAX_SUBAGENT_DEPTH = 2          # parent (depth=0) -> subagent (1) -> sub-subagent (2). Beyond this, Task refuses.
PER_SUBAGENT_TURN_CAP = 8       # plan: each subagent capped at 8 turns
MAX_SUMMARY_CHARS = 4_000       # ~4 KB summary back to parent


@dataclass(frozen=True)
class SubAgentSpec:
    """Inputs the parent gives the runner to spawn a child."""

    description: str
    prompt: str
    agent_type: str = "general"
    allowed_tools: tuple[str, ...] | None = None  # None = inherit parent registry
    max_turns: int | None = None


@dataclass
class SubAgentResult:
    summary: str
    turns: int
    tools_used: list[str] = field(default_factory=list)
    truncated: bool = False
    depth_capped: bool = False


SUBAGENT_PROMPT_TEMPLATE = """\
You are a SubAgent ({agent_type}) inside codelet.

You were spawned for a focused task and operate in an isolated context: you
cannot see the parent's conversation history or other subagents' work. Your
job is to complete the task and return a concise summary as your FINAL text
response (no tool_use). Be brief but complete.

You have access to the following tools:
{tool_list}

## Operating Rules

1. Stay focused on the task you were given; do not range beyond it.
2. Do not call the Task tool to spawn further subagents unless absolutely
   necessary -- the depth limit is {max_depth} and exceeding it is wasteful.
3. End with a plain text summary. Do not produce a final tool_use turn.

{skill_section}"""


def _shallow_copy_registry(src: ToolRegistry) -> ToolRegistry:
    """Copy parent's tool dict into a fresh registry.

    The child loop will overwrite Task/TodoWrite bindings to reference itself,
    and may unregister entries outside `allowed_tools`. We don't want those
    mutations to leak back into the parent's registry, so we hand the child a
    fresh container that points at the same Tool instances for everything else.
    """
    sub = ToolRegistry()
    for tool in src.all_tools():
        sub.register(tool)
    return sub


def _build_subagent_prompt(
    *,
    agent_type: str,
    registry: ToolRegistry,
    skill_index: SkillIndex | None,
) -> str:
    tool_list = "\n".join(
        f"- **{t.name}**: {t.description}" for t in registry.all_tools()
    ) or "(no tools)"

    skill_section = ""
    if skill_index is not None:
        summary = skill_index.index_summary()
        if summary:
            skill_section = (
                "## Skills available (fetch on demand via the `skill` tool)\n\n"
                f"{summary}"
            )

    return SUBAGENT_PROMPT_TEMPLATE.format(
        agent_type=agent_type,
        tool_list=tool_list,
        max_depth=MAX_SUBAGENT_DEPTH,
        skill_section=skill_section,
    ).strip()


class SubAgentSession:
    """Runs one isolated AgentLoop for a Task call.

    Construction stays cheap: shared registry/client/skills are passed by
    reference; only the Context and system prompt are fresh.
    """

    def __init__(
        self,
        *,
        parent_config: Config,
        parent_registry: ToolRegistry,
        client: LLMClient,
        parent_depth: int,
        skill_index: SkillIndex | None = None,
        console: Console | None = None,
        hook_runner: HookRunner | None = None,
        telemetry: Telemetry | None = None,
        parent_loop: "AgentLoop | None" = None,
    ) -> None:
        self._parent_config = parent_config
        self._parent_registry = parent_registry
        self._client = client
        self._parent_depth = parent_depth
        self._skill_index = skill_index
        self._console = console or Console()
        # Hooks are session-global; subagents inherit. Telemetry is shared so
        # subagent token spend rolls into the parent's panel.
        self._hook_runner = hook_runner
        self._telemetry = telemetry
        # Parent loop (if any) -- used to inherit plugin middleware/commands.
        self._parent_loop = parent_loop

    @property
    def parent_depth(self) -> int:
        return self._parent_depth

    @property
    def at_depth_cap(self) -> bool:
        return self._parent_depth + 1 > MAX_SUBAGENT_DEPTH

    async def run(self, spec: SubAgentSpec) -> SubAgentResult:
        if self.at_depth_cap:
            return SubAgentResult(
                summary=(
                    f"SubAgent rejected: depth cap reached "
                    f"(parent depth={self._parent_depth}, max={MAX_SUBAGENT_DEPTH})."
                ),
                turns=0,
                depth_capped=True,
            )

        # Imported here to avoid the agent_loop -> subagent -> agent_loop cycle.
        from ..agent_loop import AgentLoop

        # Hand the child a copy so unregister/replace inside the child can't
        # mutate the parent's registry.
        registry = _shallow_copy_registry(self._parent_registry)

        max_turns = spec.max_turns or min(PER_SUBAGENT_TURN_CAP, self._parent_config.max_turns)
        child_config = replace(self._parent_config, max_turns=max_turns)

        # AgentLoop applies `allowed_tools` itself (strip + rebind). We pass it
        # through so dynamic tools (Task/TodoWrite/Skill) bind to the child and
        # honor the spawn-time whitelist.
        child = AgentLoop(
            config=child_config,
            registry=registry,
            client=self._client,
            console=self._console,
            skill_index=self._skill_index,
            allowed_tools=spec.allowed_tools,
            hook_runner=self._hook_runner,
            telemetry=self._telemetry,
            _is_subagent=True,
        )
        # Inherit the parent's plugin middleware/commands so a sandbox/audit
        # plugin also wraps the subagent's tool calls (tools already come via
        # the copied registry).
        if self._parent_loop is not None:
            child.inherit_plugins_from(self._parent_loop)

        child.context = ConversationContext(config=child_config)
        child.context.depth = self._parent_depth + 1
        child.context.set_system_prompt(_build_subagent_prompt(
            agent_type=spec.agent_type,
            registry=child.registry,
            skill_index=self._skill_index,
        ))

        summary_text = await child.run_async(spec.prompt)

        truncated = False
        if len(summary_text) > MAX_SUMMARY_CHARS:
            summary_text = summary_text[:MAX_SUMMARY_CHARS] + "\n... (summary truncated)"
            truncated = True

        # Approximate turns as count of assistant messages in child context.
        turns = sum(1 for m in child.context.messages if m["role"] == "assistant")
        tools_used = sorted({
            block["name"]
            for m in child.context.messages
            if m["role"] == "assistant" and isinstance(m["content"], list)
            for block in m["content"]
            if isinstance(block, dict) and block.get("type") == "tool_use"
        })

        return SubAgentResult(
            summary=summary_text,
            turns=turns,
            tools_used=tools_used,
            truncated=truncated,
        )
