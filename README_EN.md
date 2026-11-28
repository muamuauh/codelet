<div align="center">

<img src="./assets/logo.svg" alt="codelet Logo" width="84" height="84" />

<h1>codelet</h1>

<p><strong>Lightweight, Transparent, Extensible AI Coding Agent Framework, 3-Pane Web Workbench &amp; Windows Desktop App</strong></p>

<p>
  Async Parallel Agent Loop · Native Cockpit Telemetry · 4 Modes &amp; 5 Thinking Efforts · Isolated /btw Side Q&amp;A · 2-Layer Capabilities · Windows Desktop (DPAPI Vault / Auto-Update / Explorer Context Menu)
</p>

<p>
  <img src="https://img.shields.io/static/v1?label=Version&message=v0.1.5&color=10b981&style=flat-square" alt="Version" />
  <img src="https://img.shields.io/static/v1?label=Tests&message=363+passed&color=22c55e&style=flat-square" alt="Tests" />
  <img src="https://img.shields.io/static/v1?label=License&message=MIT&color=2563eb&style=flat-square" alt="License" />
  <img src="https://img.shields.io/static/v1?label=Python&message=3.10%2B&color=0f172a&style=flat-square" alt="Python" />
  <img src="https://img.shields.io/static/v1?label=UI&message=CLI+%7C+Web+%7C+Desktop&color=0284c7&style=flat-square" alt="UI" />
  <img src="https://img.shields.io/static/v1?label=Platform&message=Windows+%7C+macOS+%7C+Linux&color=475569&style=flat-square" alt="Platform" />
</p>

<p>
  <a href="./README.md">简体中文</a> | <strong>English</strong>
</p>

<p>
  <a href="#preview">Preview</a> |
  <a href="#key-features">Key Features</a> |
  <a href="#modes--thinking-effort">Modes &amp; Effort</a> |
  <a href="#two-layer-capabilities">Plugins &amp; Skills</a> |
  <a href="#quick-start">Quick Start</a> |
  <a href="#architecture">Architecture</a> |
  <a href="#evaluations--tests">Tests</a>
</p>

</div>

---

> **Design Philosophy**: Keep the core agent loop compact and readable (~3,000 LOC) with every design trade-off documented. A minimal `AgentSink` protocol decouples `AgentLoop` from UI rendering so one engine drives both a **Rich CLI REPL** and a **zero-build 3-pane local Web Workbench**. Works natively with Anthropic and any OpenAI-compatible endpoint (DeepSeek, OpenRouter, Ollama, vLLM, etc.).

## Preview

<div align="center">
  <img src="./docs/ui.png" alt="codelet Desktop &amp; Web Workbench Preview" width="880" />
  <br />
  <sub><strong>Resizable 3-Pane Workbench &amp; Windows Desktop App · Conversations &amp; 2-Layer Capabilities · Streaming Chat &amp; Unified Composer · Cockpit / Side /btw / Tasks / Changes</strong></sub>
</div>

## Key Features

| Module | Capabilities |
| :--- | :--- |
| **Async Parallel Agent Loop** | Native `asyncio` loop dispatching multiple tool calls concurrently via `asyncio.gather` while preserving exact `tool_use` order and isolating per-tool errors; streams text & reasoning deltas (`reasoning_delta` / `thinking_delta`) with instant **Stop** cancellation. |
| **Native Cockpit Plugin** | Default-enabled `cockpit` plugin featuring project/session selector & auto-refresh toolbar, live SVG context ring + 5-segment hover breakdown + one-click `/compact`, prompt cache hit rate, TTFT/Avg/P95 latency & `tok/s` 24-turn trend charts, and provider-agnostic cross-session today / 7-day spend & model distribution charts. |
| **Windows Desktop & 3-Pane Workbench** | Standalone **Windows Desktop App** (Electron + bundled PyInstaller FastAPI sidecar — zero Python setup) with close-to-tray prompt & preference persistence, Explorer right-click **"Open with codelet"**, `electron-updater` live download progress toast, and automated categorized English Release Notes; bottom-left 4-section **Settings Center** (**General / Models & API / Plugins / About**) featuring live Chinese/English language switching, agent/context tuning, Windows DPAPI encrypted key vault, card-based provider management, declarative per-plugin `settings_schema` forms, unified composer, workspace-grouped Conversations, and per-workspace persistence for mode/effort/Plugins/Tools/Skills/fold states. |
| **Isolated `/btw` Side Q&A** | Dedicated side-panel Q&A (`Side /btw` tab & CLI `/btw`) with optional read-only main-context snapshot — **never mutates main conversation messages** and runs concurrently even while a main turn is busy. |
| **Dynamic Model Catalog** | Periodically fetches models from OpenAI-compatible endpoints (`GET /v1/models`) with local TTL disk cache, searchable picker, vendor tabs, context length / pricing / vision / reasoning badges, and automatic pricing + context-window sync. |
| **4-Layer Compaction & SubAgents** | L0 large-output spill → L1 zero-LLM old tool output clearing → L2 7-section structured rolling summary (usage-anchored, preserving todos) → L3 pair-safe hard ceiling; parallel isolated `task` subagents (max depth 2). |
| **GitHub Skill Hot-Install & Creator** | Supports both flat `*.md` and `<name>/SKILL.md` bundles (`scripts/`, `references/`); paste any GitHub URL or run `/install <url>` for frontmatter normalization, script security audit, and instant hot-reload; ships with built-in **`skill-creator`** to author new skills from natural language. |

---

## Modes & Thinking Effort

### 4 Permission Modes (`--mode` / `/mode` / UI Switcher)

| Mode | File Edits | Shell Commands (`bash`) | Best For |
| :--- | :--- | :--- | :--- |
| **`normal`** | Unified diff approval | Safe dev commands auto-run; others confirm | Everyday collaborative coding |
| **`ask`** *(default)* | Unified diff approval | All non-read-only commands confirm | Cautious changes on sensitive repos |
| **`plan`** | Hard-blocked (allowlist) | Read-only inspection commands only | Exploration & architecture planning |
| **`auto`** | Auto-approved | Auto-run (hard `denied_patterns` guard stays on) | Fast autonomous batch edits |

### 5 Thinking Effort Levels (`--effort` / `/effort` / `Think` Pill / Cockpit)

- **`auto`** *(default)* — follows the upstream model's default reasoning behavior
- **`off`** — disables reasoning (`reasoning_effort="none"` on OpenAI-compatible endpoints; omits Anthropic `thinking`)
- **`low` / `medium` / `high`** — maps to `reasoning_effort` on OpenAI-compatible endpoints and `thinking.budget_tokens` (`2048 / 6144 / 12288`) on Anthropic, with automatic fallback retry for non-reasoning models.

---

## Two-Layer Capabilities

- **Upper Layer · Plugins (`Plugins · Packs`)**: Hot-pluggable feature packs bundling `Tools + Skills + Commands + Dynamic UI Panels + Middleware + TurnPolicy`. Features **reversible `PluginHandle.dispose()` / `ctx.effect()`** in-place hot-plugging (zero `AgentLoop` rebuild), optional **`requires` topological ordering + cascade unmount**, and directory bundles (`.codelet/plugins/<name>/`).
  - Built-in plugins: **`cockpit`** *(default on)*, **`mcp`** *(stdio & HTTP/SSE MCP bridge)*, **`rag`**, **`memory`**, **`evolve`** *(with `create_plugin_pack` Creator mode)*, **`sandbox`**, **`router`**.
- **Base Layer · Capabilities (`Tools` & `Skills`)**: Fine-grained toggles grouped by origin (`Core / From plugins / Self-evolved` for tools; `Built-in / From plugins / Standalone · Project / Standalone · Global` for skills). Ships with built-in **`skill-creator`** and **`plugin-creator`** skills.

---

## Quick Start

### Option A: Download Windows Desktop App (Recommended, Zero Setup)

Download from [GitHub Releases](https://github.com/muamuauh/codelet/releases/latest):
- **`codelet-Setup-x.y.z.exe`** *(Recommended)* — Per-user NSIS installer (no admin required, Start Menu/Desktop shortcuts, Explorer right-click **"Open with codelet"**, fastest startup, automatic GitHub Releases update checks).
- **`codelet-x.y.z-portable.exe`** — Single-file portable executable.
- **`codelet-x.y.z-win-x64-unpacked.zip`** — Extract and run `codelet.exe` directly.

Click the bottom-left **Settings (⚙)** button to open the Settings Center — configure providers & API Keys (stored encrypted via Windows DPAPI), switch UI language (Chinese / English) and close-to-tray behavior under **General**, toggle and configure individual plugins under **Plugins**, and check for updates under **About**.

### Option B: Run from Source (CLI / Web / Desktop Dev)

```bash
conda create -n codelet python=3.10 -y
conda activate codelet
pip install -e ".[dev,web]"

cp .env.example .env
# Edit .env: set LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
```

```bash
# Launch local 3-pane Web Workbench
python -m codelet.web

# Launch interactive CLI REPL
python -m codelet

# One-shot prompt with mode & thinking effort
python -m codelet --mode normal --effort medium "Audit TODOs across this repo"
```

---

## Architecture

```mermaid
flowchart LR
    subgraph UI["UI Layer (via AgentSink)"]
        CLI["Rich CLI REPL"]
        Web["3-Pane Web Workbench<br/>FastAPI + WebSocket + Vanilla JS"]
    end

    subgraph Core["Core Engine (codelet/)"]
        Loop["AgentLoop<br/>Parallel Dispatch · Hot-Reload · /btw"]
        Gate["PermissionGate + Hooks"]
        Ctx["ConversationContext<br/>L0-L3 Compaction · 5-Segment Breakdown"]
        Tel["Telemetry<br/>Cache · TTFT · P95 · tok/s · Cost"]
    end

    subgraph Caps["2-Layer Capabilities"]
        Plugins["Plugins · Packs (Reversible)<br/>cockpit / mcp / rag / memory / evolve / sandbox / router"]
        Atoms["Base Capabilities<br/>Core Tools + Skills (builtin / standalone / GitHub)"]
    end

    subgraph LLM["LLM Abstraction"]
        Catalog["ModelCatalog<br/>TTL Disk Cache + Pricing Sync"]
        Clients["AnthropicClient / OpenAICompatClient<br/>Streaming · Reasoning · Effort"]
    end

    CLI <--> Loop
    Web <--> Loop
    Loop --> Gate
    Loop --> Ctx
    Loop --> Tel
    Plugins --> Atoms
    Loop --> Plugins
    Loop --> Atoms
    Loop <--> Clients
    Catalog -.->|"context_window / pricing"| Loop
```

---

## Evaluations & Tests

```bash
pytest -q                                        # 361 passed
python -m evals.runner                           # local task eval suite
python -m evals.swebench.run_swebench --gold --limit 5
```

Deep-dive docs (in Chinese): [USAGE.md](./USAGE.md) · [docs/architecture.md](./docs/architecture.md) · [docs/plugin-architecture.md](./docs/plugin-architecture.md) · [docs/flow-diagrams.md](./docs/flow-diagrams.md) · [docs/technical-details.md](./docs/technical-details.md) · [docs/interview-qa.md](./docs/interview-qa.md).

---

## License

MIT © 2026 [muamuauh](https://github.com/muamuauh)
