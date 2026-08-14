# Plugin Architecture (P8)

## Status: implemented

Built (`codelet/plugins/`): `register_tool` (name collision overrides a core tool),
`add_system_prompt_section`, `on_user_prompt` (prompt middleware), `wrap_tool`
(async tool-execution middleware), and `register_command` (a `/name` slash command
returning a status string — wired into both the CLI and the web GUI). Discovery is
entry points (`codelet.plugins`) + `./.codelet/plugins/*.py` + `~/.codelet/plugins/*.py`,
curated by the `plugins` block in settings.json; failures are isolated. **Sub-agents
inherit** the parent's tool/prompt middleware, sections, and commands (plus the
plugin-registered tools via the copied registry). Not yet built: provider registration.

### Built-in plugins (ship with codelet; load only when enabled)

Two reference plugins live in `codelet/plugins/builtin/`. They are **never
auto-discovered** — enable them by name in settings.json:

```json
{"plugins": {"enabled": ["sandbox", "rag"],
             "config": {"sandbox": {"image": "python:3.12-slim", "network": false},
                        "rag": {"inject": false, "top_k": 3}}}}
```

- **sandbox** — overrides `bash` to run each command in a throwaway Docker
  container with the workspace bind-mounted at `/workspace` and (by default) no
  network. Real container isolation; needs Docker; each call is a fresh container
  (chain dependent steps with `&&`).
- **rag** — a `search_docs(query, k)` tool (BM25 over the workspace's file chunks,
  pure-Python, no embedding API), a `/rag` command that rebuilds the index and
  reports stats, and — with `"inject": true` — a prompt middleware that prepends the
  top hits to each message. The index rebuilds when the workspace changes.

Minimal working plugin — drop into `.codelet/plugins/audit.py`:

```python
from codelet.plugins import PluginContext

class AuditPlugin:
    name = "audit"
    def setup(self, ctx: PluginContext) -> None:
        ctx.add_system_prompt_section("An audit plugin is logging every tool call.")
        async def wrap(name, tool_input, call_next):
            print(f"[audit] {name} {tool_input}")
            return await call_next()
        ctx.wrap_tool(wrap)

PLUGIN = AuditPlugin()   # module-level PLUGIN is what the loader looks for
```

Enable/curate in settings.json (optional — discovered plugins load by default):
`{"plugins": {"enabled": ["audit"], "config": {"audit": {}}}}`.

## Context

codelet already has four disconnected extension seams: **tools** (`ToolRegistry`),
**hooks** (shell commands on `PreToolUse`/`PostToolUse`/`UserPromptSubmit`),
**skills** (markdown knowledge), and **LLM providers** (`llm/factory`). Adding a
capability like a **sandbox** (run bash/writes inside a container) or **RAG**
(retrieve + inject context) today means editing the core in several places.

Goal: one first-class **plugin** interface that bundles those contributions, is
`pip install`-able or drop-in, and can be enabled per-project — without bloating
the ≤3000-line core. Inspired by harness-style pluggable backends
(deepseek-harness). This is a plan only; nothing here is built yet.

## What a plugin can contribute

A plugin is a small object that, given a `PluginContext`, registers any of:

| Contribution | API on `ctx` | Powers |
|---|---|---|
| Tools | `ctx.register_tool(tool)` | RAG `search_docs`, a sandbox exec tool |
| Tool override | same (name collision replaces) | sandbox replacing `bash`/`write_file` |
| Tool middleware | `ctx.wrap_tool(fn)` | audit, redaction, sandbox routing |
| Prompt middleware | `ctx.on_user_prompt(fn)` | RAG context injection, guardrails |
| System-prompt section | `ctx.add_prompt_section(fn)` | expose plugin capabilities to the model |
| LLM provider | `ctx.register_provider(name, factory)` | new backends |
| Slash command | `ctx.register_command(name, fn)` | `/index`, `/sandbox status` |
| Config | `ctx.config` | plugin settings from `settings.json` |

```python
# codelet/plugins/base.py  (proposed)
class Plugin(Protocol):
    name: str
    def setup(self, ctx: "PluginContext") -> None: ...
    def shutdown(self) -> None: ...      # optional; tear down containers, indexes
```

## Discovery & enablement

`load_plugins()` (new `codelet/plugins/loader.py`) collects, in order:
1. **Entry points** — `importlib.metadata.entry_points(group="codelet.plugins")`
   (pip-installable third-party plugins).
2. **Local** — `.codelet/plugins/*.py` (project) and `~/.codelet/plugins/*.py`
   (user), each exposing a `PLUGIN` object — mirrors how skills/commands load.
3. Filtered/ordered by `settings.json`:
   ```json
   {"plugins": {"enabled": ["sandbox", "rag"], "config": {"sandbox": {"image": "python:3.12"}}}}
   ```
Unknown/failed plugins log a warning and are skipped — never crash the agent
(same contract as hooks/compaction).

## Integration points (small, localized core edits)

- `AgentLoop.__init__`: after building the registry, call `apply_plugins(self, ...)`
  which runs each `plugin.setup(ctx)`; contributed tools land in `self.registry`,
  middlewares/sections are stored on the loop.
- `build_system_prompt`: accept extra `sections` and append them (RAG capabilities,
  sandbox notice).
- `run_async` prompt path: run `on_user_prompt` middlewares (Python analog of the
  shell `UserPromptSubmit` hook) before appending the user message.
- `_dispatch_one`: wrap `tool.aexecute` in the `wrap_tool` middleware chain — this
  is where a sandbox reroutes execution and an auditor observes it. The existing
  shell hooks stay; plugin middleware is the richer in-process sibling.
- Sub-agents inherit the parent's applied plugins by reference (like skills/hooks).

## Worked example: sandbox plugin

`register_tool` an execution backend that overrides `bash` (and optionally
`write_file`/`edit_file`) to run inside a container (`docker exec` / a restricted
subprocess), reusing the WSL/Docker plumbing already proven by the SWE-bench work.
`wrap_tool` denies host-escaping paths; `add_prompt_section` tells the model it is
sandboxed. Config: image, mounts, network on/off. `shutdown` removes the container.

## Worked example: RAG plugin

`register_tool` a `search_docs(query)` retriever over a local index; a `/index`
slash command builds/refreshes it; an `on_user_prompt` middleware optionally
prepends the top-k snippets as context (bounded, and compaction-aware). Index and
embeddings backend are plugin-config; nothing leaks into the core.

## Phasing

1. `plugins/base.py` (`Plugin`, `PluginContext`) + `plugins/loader.py` (entry
   points + local dirs + settings enable-list) + `apply_plugins`. Tools + prompt
   sections only. Ship one trivial example plugin + tests.
2. Middlewares: `on_user_prompt` and `wrap_tool` chains wired into the loop.
3. Provider + slash-command registration; sub-agent inheritance.
4. Reference plugins: `codelet-sandbox`, `codelet-rag` as separate optional
   packages (own `[sandbox]` / `[rag]` extras), proving the entry-point path.

## Testing & safety

- Unit: a fake plugin registers a tool + a prompt section + a `wrap_tool` and the
  loop reflects all three; a broken plugin is skipped with a warning.
- Isolation: middleware order deterministic; a plugin exception never kills a turn.
- Trust: plugins run arbitrary Python (like hooks). Enablement is explicit in
  `settings.json`; document the trust boundary and keep discovery opt-in per project.

## Non-goals (for now)

Hot-reload, a plugin marketplace/versioning, cross-plugin dependency resolution.
Keep v1 static (loaded at startup), matching the rest of codelet.
