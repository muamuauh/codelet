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

Three reference plugins live in `codelet/plugins/builtin/`. They are **never
auto-discovered** — enable them by name in settings.json:

```json
{"plugins": {"enabled": ["sandbox", "rag", "evolve"],
             "config": {"sandbox": {"image": "python:3.12-slim", "network": false},
                        "rag": {"inject": false, "top_k": 3},
                        "evolve": {"dir": ".codelet/evolved"}}}}
```

- **sandbox** — adds a separate `sandbox` tool that runs each command in a
  throwaway Docker container with the workspace bind-mounted at `/workspace` and
  (by default) no network. The core `bash` (host) stays; the model picks per
  command. Real container isolation; needs Docker; each call is a fresh container
  (chain dependent steps with `&&`).
- **rag** — a `search_docs(query, k)` tool (BM25 over the workspace's file chunks,
  pure-Python, no embedding API), a `/rag` command that rebuilds the index and
  reports stats, and — with `"inject": true` — a prompt middleware that prepends the
  top hits to each message. The index rebuilds when the workspace changes.
- **evolve** — self-evolution: a `create_tool` meta-tool that lets the agent author
  a new tool mid-conversation and hot-load it into the running session. See
  [Self-evolution](#self-evolution-the-agent-grows-its-own-tools) below.

## Self-evolution: the agent grows its own tools

The **evolve** plugin closes the loop on the plugin system: instead of a human
writing a plugin file, the *agent* writes one when it hits a capability it lacks,
and it takes effect immediately.

**Flow.** The plugin registers one meta-tool, `create_tool`, bound to the live
`AgentLoop` (it reaches the loop through the new `ctx.host` reference). When the
model calls it with `name` / `description` / `parameters` (a JSON-schema object) /
`code` (a Python function body over a `params` dict), the tool:

1. Validates the name (must be a fresh snake_case identifier; core tools are
   protected from being overridden by improvisation).
2. Renders a complete, readable plugin module — a `Tool` subclass wrapping the
   authored body plus a module-level `PLUGIN` — and `compile()`s it to reject
   syntax errors before anything touches disk.
3. Writes it to the evolved directory (`.codelet/evolved/<name>.py` by default).
4. Calls `host.activate_plugin_file(path)`, which loads that one file and applies
   it to the **live** registry, merges any prompt sections / middleware / commands,
   and rebuilds the system prompt — so the new tool is callable on the agent's very
   next turn, no restart.

On startup the plugin re-loads every file in the evolved directory, so authored
tools **persist** across sessions. `/evolve` lists them. Each evolved file is a
plain plugin module you can read, edit, move into `.codelet/plugins/`, or delete.

**Safety.** Self-authored code runs in-process with full privileges, so the plugin
is opt-in (built-in, loads only when named in `plugins.enabled`) and off by default.
In **ASK** mode `create_tool` previews the *generated source* through the normal
diff-approval flow (it is in `_DIFF_CONFIRM_TOOLS`), so a human vets the code before
it is written and loaded. The evolved directory is git-ignored by default — copy a
tool into the repo deliberately to keep it. A broken or buggy evolved tool is
isolated: a syntax error is reported back to the model to fix, a runtime exception
is caught and returned as a tool error, and a file that fails to import at startup
is skipped with a warning instead of crashing the agent.

```
User: "summarize the word frequencies in README.md"
Agent → create_tool(name="word_freq", parameters={... "path" ...},
                    code="import collections, re; ...; return ...")
        ← "Created and activated tool 'word_freq'. It is now available…"
Agent → word_freq(path="README.md")   # called on the next turn
```

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

A plugin marketplace/versioning and cross-plugin dependency resolution. Third-party
plugins stay static (loaded at startup); the one dynamic path is **self-evolution**
(the evolve plugin hot-activates tools the agent authors into its own evolved
directory — see above), which is deliberately scoped to that opt-in subsystem rather
than general hot-reload of arbitrary plugins.
