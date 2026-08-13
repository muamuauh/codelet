# codelet — Implementation Plan

## Context

本项目是一个轻量级 AI 编码助手框架，核心是异步 agent loop + 工具系统 + 权限门控，外加一系列工程化能力：SubAgent（隔离 fork）、并行执行、Skills、Hooks、上下文自动压缩、Token/成本遥测、多 LLM provider（Anthropic / OpenAI 兼容的任意中转站）、Session 持久化、Slash 命令模板、Diff 预览。

工作目录：[d:\codes\my-codelet](d:/codes/my-codelet/)。

**目标边界**：核心代码 ≤ ~3000 行，保持模块清晰可读（"Agent Loop 是灵魂、工具是双手、权限是护盾"），不引入 enterprise 复杂度（不做 MCP、不做 worktree 沙盒、不做 5 层权限模型）。

**用户已确认的关键决策**：
- 语言：**Python 3.10+**（用 asyncio 实现并行）
- 运行环境：**miniconda**，环境名 `codelet`（所有运行/测试/装包统一在该环境内进行；不使用 venv/uv 创建新环境）
- LLM：**Anthropic 原生 + OpenAI 兼容**双轨抽象（DeepSeek/Ollama/SiliconFlow 即插即用）
- 附加创新：**全要** — Hooks + Context 压缩 + Token 遥测 + TodoWrite + Slash 命令 + diff 预览 + Session 持久化 + WebFetch
- subagent 递归深度上限：**2 层**

**环境约定**：
- 创建：`conda create -n codelet python=3.10 -y`（若尚不存在）
- 激活：`conda activate codelet`
- 安装依赖：在激活的环境内 `pip install -e .`（项目用 `pyproject.toml` 描述依赖）
- 运行/测试：所有 `python -m codelet` / `pytest` 命令都默认在此环境下执行

---

## 目录结构

```
d:\codes\my-codelet\
├── pyproject.toml                # 在 miniconda env=codelet 内 pip install -e .；deps: anthropic, openai, rich, pyyaml, httpx
├── README.md                     # 项目说明 + 功能概览
├── CLAUDE.md                     # 项目 memory（运行时自动加载）
├── .codelet/
│   ├── settings.json             # 权限规则 + hooks + 默认 model
│   ├── skills/                   # 项目级 skill（覆盖用户级）
│   └── commands/                 # 项目级 slash 命令
├── codelet/
│   ├── __init__.py
│   ├── __main__.py               # python -m codelet
│   ├── cli.py                    # REPL + Rich 渲染 + slash 分发
│   ├── config.py                 # Settings dataclass + settings.json 分层加载
│   ├── system_prompt.py          # 基础 prompt + skill 索引 + memory 注入
│   ├── agent_loop.py             # 异步 turn loop + 并行 dispatch + hooks 调用
│   ├── context.py                # 消息缓冲 + token 计费 + 压缩触发
│   ├── permissions.py            # 2 层 + settings.json allow/deny
│   ├── telemetry.py              # token / 成本 / 调用次数 累计
│   ├── llm/
│   │   ├── base.py               # LLMClient ABC（chat / stream / count_tokens）
│   │   ├── anthropic_client.py   # 原生 Anthropic
│   │   └── openai_compat.py      # OpenAI 兼容（含 tool-use 语义适配）
│   ├── tools/
│   │   ├── base.py               # Tool ABC + Registry（execute 改 async）
│   │   ├── bash_tool.py
│   │   ├── file_read.py
│   │   ├── file_write.py         # 含 diff 预览 + ASK 模式确认
│   │   ├── file_edit.py          # 含 diff 预览 + ASK 模式确认
│   │   ├── glob_tool.py
│   │   ├── grep_tool.py
│   │   ├── todo_write.py         # 内存 todo 列表，rich 面板渲染
│   │   ├── skill_tool.py         # 按需取出 skill body
│   │   ├── task_tool.py          # 派发 subagent（支持并行）
│   │   ├── web_fetch.py          # httpx 抓 URL + 简单 HTML→text
│   │   └── web_search.py         # 占位 / 可选
│   ├── skills/
│   │   └── loader.py             # 解析 frontmatter md，构建 name→body 索引
│   ├── subagent/
│   │   └── runner.py             # SubAgentSession（独立 context，带深度上限）
│   ├── hooks/
│   │   └── runner.py             # PreToolUse / PostToolUse / UserPromptSubmit
│   ├── persistence/
│   │   └── session.py            # ~/.codelet/sessions/{id}.json
│   └── slash/
│       └── loader.py             # 解析 ./commands/*.md → 展开成 prompt
└── tests/
    ├── test_agent_loop.py
    ├── test_parallel_dispatch.py
    ├── test_subagent.py
    ├── test_skill_loader.py
    ├── test_hooks.py
    ├── test_compaction.py
    └── test_e2e.py               # 桩 LLM，串起 subagent+并行+skill
```

预估：核心 ~2400 行 + 测试 ~600 行，总 ~3000 行。

---

## 关键模块设计

### 1. LLM 抽象层（[llm/base.py](../codelet/llm/base.py)）

```python
class LLMClient(ABC):
    async def chat(messages, system, tools, **kwargs) -> Response
    def normalize_tool_call(raw) -> dict       # 统一成 {id, name, input}
    def normalize_tool_result(id, content, is_error) -> dict
```

- Anthropic 客户端直接透传。
- OpenAI 兼容客户端做两件事：(a) 把 `tools` 字段从 Anthropic schema 转成 OpenAI function-call schema；(b) 把 `tool_calls` / `tool` 角色消息互相翻译。
- 所有上层代码只见 `LLMClient` 和 Anthropic 风格的内部消息格式。

### 2. 异步 agent loop + 并行调度（[agent_loop.py](../codelet/agent_loop.py)）

- 工具 `execute()` 全部改成 `async`；阻塞型工具（Bash、文件 IO）内部用 `asyncio.to_thread`。
- 单 turn 拿到 N 个 `tool_use` 块时：
  ```python
  results = await asyncio.gather(
      *[dispatch(call) for call in tool_calls],
      return_exceptions=True,
  )
  ```
- **顺序保持**：用 `tool_use_id` 作为 key 收集结果，按 LLM 原始 emit 顺序回填 `tool_result` 块（Anthropic API 强制要求）。
- **错误隔离**：每个任务异常包成 `is_error=true` 的 tool_result，兄弟继续。

### 3. Subagent（[subagent/runner.py](../codelet/subagent/runner.py) + [tools/task_tool.py](../codelet/tools/task_tool.py)）

- 工具 schema：`Task(description, prompt, agent_type="general", allowed_tools?: list[str])`，返回单个 summary 字符串。
- `SubAgentSession` 持有：**独立** Context（空消息列表）、自定义 system prompt（不含父历史）、共享的 ToolRegistry / LLMClient / SkillIndex；权限模式默认继承父级，可收紧。
- 结果协议：subagent 跑完自己的 loop，最后一条 assistant 文本即 summary，截断到 ~4 KB；附带 `metadata: {turns, tokens, tools_used}` 通过 stderr 风格的注释加到父 context。
- **递归上限 = 2**：`task_tool` 检查 `ctx.depth < 2`，超出直接返回 error 字符串，不抛异常。
- 父级在一个 turn 中可以并行派发多个 Task，由 §2 的 `asyncio.gather` 自然支持。

### 4. Skill 系统（[skills/loader.py](../codelet/skills/loader.py) + [tools/skill_tool.py](../codelet/tools/skill_tool.py)）

- **格式**：markdown + YAML frontmatter
  ```markdown
  ---
  name: python-lint-review
  description: Review Python code for lint, unused imports, type issues
  triggers: [lint, review, python]
  allowed_tools: [Bash, Read, Grep]   # 可选
  ---
  # body...
  ```
- **发现顺序**：`./.codelet/skills/*.md`（项目级，优先）覆盖 `~/.codelet/skills/*.md`（用户级），启动时一次性加载。
- **暴露方式（关键设计）**：system prompt 里只放紧凑索引（`name: description` 单行，每条 ~80 字符）；`Skill(name)` 工具按需取出 body。这样冷上下文小，模型自己决定何时拉取。
- **三者边界**：
  - **Tool** = 有副作用的能力（执行）
  - **Skill** = 可按需注入的程序性知识（不执行，只指导）
  - **Subagent** = 带独立 loop 的隔离执行（fork）

### 5. Hooks（[hooks/runner.py](../codelet/hooks/runner.py)）

- 事件：`PreToolUse` / `PostToolUse` / `UserPromptSubmit`。
- settings.json 配置：
  ```json
  {
    "hooks": {
      "PreToolUse": [{"matcher": "Bash", "command": "echo blocked && exit 2"}]
    }
  }
  ```
- 协议：hook 命令通过 stdin 接收 JSON 事件，stdout 可输出修改后的 input（PreToolUse），exit code != 0 表示阻断；30 秒超时。

### 6. Context 压缩（[context.py](../codelet/context.py)）

- 触发条件：估算 token 数 > 模型窗口 75% 时。
- 策略：把最旧的前半段消息打包，调用 Haiku 生成 ~500 token 摘要，替换为单条 `<conversation_summary>` system 块。
- token 估算用 `LLMClient.count_tokens()`（Anthropic 有原生接口；OpenAI 兼容用 tiktoken 估算）。

### 7. 持久化、Slash、Diff 预览、Telemetry（小件）

- **Session**：每条用户/assistant 消息追加进 `~/.codelet/sessions/{id}.json`；`/resume <id>` 重建 context。
- **Slash 命令**：`./.codelet/commands/<name>.md`（带可选 frontmatter）→ `/name args` 展开成 prompt 注入。
- **Diff 预览**：FileWrite/FileEdit 在 ASK 模式下先用 `difflib.unified_diff` 显示彩色 diff，y/n 确认。
- **Telemetry**：每轮在右侧 rich panel 显示 `in/out tokens · turn cost · cumulative cost`；价格表写死在 `telemetry.py` 顶部常量。

---

## 实施分阶段

| Phase | 内容 | 增量 LOC | 关键文件 |
|---|---|---|---|
| **P1** 忠实克隆 + LLM 抽象 | 6 个原工具、agent loop、2 层权限、context、REPL；引入 `LLMClient` 但只发 Anthropic 实现 | ~750 | [agent_loop.py](../codelet/agent_loop.py), [tools/base.py](../codelet/tools/base.py), [llm/anthropic_client.py](../codelet/llm/anthropic_client.py) |
| **P2** 异步化 + 并行调度 | 工具改 async，`asyncio.gather` 派发，结果按 tool_use_id 排序回填 | ~250 | [agent_loop.py](../codelet/agent_loop.py) |
| **P3** Subagent + Skill + TodoWrite | task_tool / SubAgentSession / skill loader / Skill 工具 / TodoWrite | ~550 | [subagent/runner.py](../codelet/subagent/runner.py), [skills/loader.py](../codelet/skills/loader.py) |
| **P4** Hooks + 压缩 + Telemetry + Rich TUI | settings.json + 3 个 hook 事件 + 压缩 + 成本面板 | ~400 | [hooks/runner.py](../codelet/hooks/runner.py), [context.py](../codelet/context.py), [telemetry.py](../codelet/telemetry.py) |
| **P5** OpenAI 兼容 + WebFetch + Session + Slash + Diff 预览 | OpenAI 客户端、httpx 抓页、JSON 持久化、./commands/、写文件 diff 确认 | ~500 | [llm/openai_compat.py](../codelet/llm/openai_compat.py), [persistence/session.py](../codelet/persistence/session.py) |

每个 Phase 都以"全部测试通过 + 一个 demo 跑通"作为 done 条件。

---

## 风险与缓解

1. **并行 tool_result 顺序错乱导致 API 静默失败** — Anthropic 要求 `tool_result` 顺序与 `tool_use` 严格对应，错位会抛 400 但错误信息晦涩。**缓解**：把 dispatch 抽成单一函数 `dispatch_parallel(tool_calls) -> list[tool_result]`，写专门测试故意让任务以乱序完成，断言输出顺序与输入一致。
2. **Subagent token 爆炸** — 递归 + 每个独立 context 会让 token 累积失控。**缓解**：深度硬上限 = 2，每个 subagent turn 上限 = 8，telemetry 显示 subagent token 占比；超阈值在 stderr 警告。
3. **Skills 膨胀污染 system prompt** — 用户装 50 个 skill 后每轮都贵。**缓解**：索引只放 `name: description` 单行（~80 字符），settings 加 `max_skills_in_index` 上限；body 永远在 `Skill` 工具背后。
4. **OpenAI 兼容协议适配漏洞** — function-call vs tool-use 语义不一致（如多轮 tool_use 排序、parallel tool calls 支持差异）。**缓解**：在 `openai_compat.py` 顶部明确支持矩阵；不支持并行的 provider 自动 fallback 到串行 dispatch。

---

## 端到端验证

**示范任务**：`"审计这个 repo：找出所有 TODO 注释和未使用的 import，每条给出修复建议。"`

预期执行路径：
1. 一个 turn 内 LLM 同时发出 `Glob("**/*.py")` 和 `Grep("TODO")` → 验证 **并行 dispatch**
2. LLM 调用 `Skill(name="python-lint-review")` → 验证 **skill 按需加载**
3. LLM 并行派发两次 `Task(prompt="for files in <list>, list unused imports")` → 验证 **subagent 并行 + 上下文隔离**
4. 每个 subagent 内部用 Read + Bash(ast-grep) 工作，返回 ~2 KB summary
5. 父级用 `TodoWrite` 列出修复项，rich 表格渲染；telemetry 面板显示 token 花销
6. 用户输入 `/exit`，session 持久化到 `~/.codelet/sessions/`；下次 `/resume <id>` 可恢复

**自动化测试**（[tests/test_e2e.py](../tests/test_e2e.py)）：用桩 LLMClient 脚本化上述工具序列，断言：
- 并行结果按 tool_use 原顺序回填
- subagent context 不泄漏父级消息
- skill body 仅在显式 fetch 时进入 subagent context
- 第三层 Task 被深度上限拒绝
- session JSON 可往返序列化

---

## 关键文件（实施时主修改）

- [codelet/agent_loop.py](../codelet/agent_loop.py) — 异步主循环 + 并行派发
- [codelet/subagent/runner.py](../codelet/subagent/runner.py) — SubAgentSession，深度/turn 上限
- [codelet/tools/task_tool.py](../codelet/tools/task_tool.py) — Task 工具
- [codelet/skills/loader.py](../codelet/skills/loader.py) — skill 加载与索引
- [codelet/tools/base.py](../codelet/tools/base.py) — async Tool ABC + Registry
- [codelet/llm/base.py](../codelet/llm/base.py) — LLMClient 抽象
- [codelet/hooks/runner.py](../codelet/hooks/runner.py) — hooks 执行器
- [codelet/context.py](../codelet/context.py) — 含压缩
- [.codelet/settings.json](../.codelet/settings.json) — 项目级配置示例
