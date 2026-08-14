# codelet

一个轻量级 AI 编码助手框架。核心是异步 agent loop + 工具系统 + 权限门控，外加一整套
工程化能力：**SubAgent / 并行执行 / Skill 系统 / Hooks / 上下文自动压缩 / Token & 成本
遥测 / 多 LLM 提供方（Anthropic / OpenAI / DeepSeek / 任意 OpenAI 兼容中转站）/
Session 持久化 / Slash 命令模板 / Diff 预览**。

设计目标：核心代码 ≤ ~3000 行，每一处都讲清楚为什么这样写。

> 🚀 详细使用见 [USAGE.md](USAGE.md)（Windows + miniconda 实操指南）。

📚 **文档**（[docs/](docs/) 下的索引见 [docs/README.md](docs/README.md)）：

- [架构图](docs/architecture.md) — 模块责任划分 + 扩展点
- [运行时流程图](docs/flow-diagrams.md) — 9 张 mermaid 时序图覆盖关键路径
- [技术细节深入](docs/technical-details.md) — 12 个有坑的实现点
- [面试式 Q&A](docs/interview-qa.md) — 30 个设计权衡问题
- [实现路线图](docs/implementation-plan.md) — P1→P5 phase 划分

## 当前进度

- [x] **P1** 核心骨架 + LLM 抽象层（6 工具、agent loop、2 层权限、context、Rich REPL）
- [x] **P2** 异步化 + 并行 dispatch（`Tool.aexecute` + `asyncio.gather` + 顺序保持 + 错误隔离）
- [x] **P3** Subagent + Skill + TodoWrite（深度上限 2、上下文隔离、并行 Task、按需 skill 加载）
- [x] **P4** Hooks + Context 压缩 + Token/成本 Telemetry + settings.json 分层加载
- [x] **P5** 多 provider profiles（Anthropic / OpenAI 兼容）+ `.env` 加载 + WebFetch + Session 持久化 + Slash 命令模板 + Diff 预览
- [x] **P6** 实时 token 流式输出 + 等待 spinner + per-project session registry（分区 `/sessions`）+ 执行式 eval 框架 + SWE-bench Lite 适配器
- [x] **P7** 本地 Web GUI（FastAPI + WebSocket + 原生前端）：流式聊天 + 工具卡片 + diff 审批 + 会话/画像/遥测，核心经 `AgentSink` 解耦
- [x] **P8** 插件系统（tool / prompt-中间件 / tool-中间件 / slash 命令 / system-prompt 段；entry-point + `.codelet/plugins/` 发现，subagent 继承；内置 **sandbox**(Docker 隔离 bash) 与 **rag**(BM25 检索) 插件，见 [docs/plugin-architecture.md](docs/plugin-architecture.md)）+ 图片多模态（Web 上传图片 → vision content block → OpenAI/Anthropic 客户端翻译）

测试：`182 passed`（`pytest -q`）。

## 环境

```bash
conda create -n codelet python=3.10 -y
conda activate codelet
pip install -e ".[dev]"            # 本地开发
# pip install -e ".[dev,openai,web]"  # P5 之后再启用
```

## 配 LLM 的最快路径：(base_url, api_key, model) 三元组

只要在 `.env` 里写三个变量就能跑任意 OpenAI 兼容的端点（DeepSeek / OpenRouter /
SiliconFlow / Moonshot / vLLM / Ollama / 自建中转 / 任何兼容服务）：

```bash
cp .env.example .env
```

```ini
# .env
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=deepseek-chat
# LLM_PROVIDER=openai   # 默认；想用 Anthropic 原生时设 "anthropic"
```

跑：

```bash
python -m codelet "你好"
```

不需要碰 `settings.json`，不需要 `--profile`。换一个中转就只改这三行。

> Shell exported 的环境变量永远盖 `.env`，所以 CI / 部署里的 secret 管理工具不会被
> `.env` 文件干扰。

### 一次性传给 CLI（不写文件）

```bash
python -m codelet \
  --base-url https://api.deepseek.com/v1 \
  --api-key sk-xxx \
  --model deepseek-chat \
  "你好"
```

`--base-url` 给定但没显式 `--provider` 时自动推断为 `openai` 兼容（Anthropic 几乎不需要
改 base_url）。

### 想要切来切去 → 命名 profile

如果你常在多个 LLM 之间切，把它们放进
[`.codelet/settings.json`](.codelet/settings.json) 的 `profiles`：

```json
{
  "profiles": {
    "myproxy": {
      "provider": "openai",
      "base_url": "https://your-proxy.example/v1",
      "model": "your-model",
      "api_key_env": "MYPROXY_API_KEY"
    },
    "myproxy-inline": {
      "provider": "openai",
      "base_url": "https://your-proxy.example/v1",
      "model": "your-model",
      "api_key": "sk-paste-key-directly"
    }
  }
}
```

每条 profile 二选一：
- `api_key_env: "X"` → 从 `.env` / shell 里读 `X`（推荐，不把 secret 写进版本库）
- `api_key: "sk-..."` → 直接写明文（self-contained，单文件可分发）

切换：`python -m codelet --profile myproxy "..."`

### 解析优先级

```
CLI flags  >  --profile X  >  settings.profile  >  LLM_*(env-driven)  >  "anthropic"
```

`/profile` 命令实时显示当前激活的四元组。

## 运行

```bash
# 交互式 REPL（用 .env 里的 LLM_* 三元组，或没设的话用 anthropic profile）
python -m codelet

# 一次性 prompt
python -m codelet "在当前目录找出所有 TODO 注释"

# 切预置 profile
python -m codelet --profile deepseek
python -m codelet --profile openai-gpt4o
python -m codelet --profile ollama-local

# 切换权限模式
python -m codelet --mode auto "..."

# 恢复上次会话
python -m codelet --resume 20260503-153045-ab12
```

REPL 内置命令：
`/tools` `/skills` `/commands` `/todos` `/usage` `/profile`
`/sessions` `/resume <id>` `/save`
`/mode [ask|auto|plan]` `/help` `/quit`。

用户自定义命令（项目里已带 `/audit` 示例，见
[.codelet/commands/audit.md](.codelet/commands/audit.md)）放在
`./.codelet/commands/<name>.md` 或 `~/.codelet/commands/<name>.md`，
通过 `/<name> args` 调用。命令体支持 `{args}` `{1}` `{2}` 占位。

## Web GUI（图形界面，P7）

一个本地 Web 界面,浏览器里聊天,和 REPL 同一套 `AgentLoop`:

```bash
pip install -e ".[web]"
python -m codelet.web            # 起服务并自动打开浏览器（--no-open 关掉）
# 或一键脚本： ./run-web.ps1        (Windows)   /   ./run-web.sh   (macOS/Linux)
```

- **流式输出**:token 实时逐字显示(WebSocket)
- **模型切换**:顶栏下拉直接换模型(列表来自 `.env` 的 `LLM_MODELS`;代理场景下 base_url/api_key 不变,只换模型名)
- **新建对话 / 选工作区**:侧栏 `＋ New chat` 开新会话;`Workspace → Change` 弹目录选择器,切到别的项目目录(agent 的文件工具、`.codelet` skills/CLAUDE.md、会话都随之切换)
- **工具可见**:每次工具调用渲染成卡片(参数 + 结果,按成功/失败着色)
- **权限审批**:ASK 模式下 `write_file`/`edit_file` 弹 **diff 预览** + Approve/Reject;AUTO 模式直接执行。顶栏可实时切换
- **会话/遥测**:侧栏列出当前项目的会话(点击恢复);顶栏看 token/成本
- 纯原生 HTML/JS,无构建链;只绑 `127.0.0.1`(本地)

实现上,核心 `AgentLoop` 通过一个极小的 [`events.py`](codelet/events.py) `AgentSink`
把 UI 事件解耦出来:CLI 用 `RichSink`(终端输出不变),Web 用把事件序列化成
WebSocket 帧的 sink。diff 审批的回调支持 async(浏览器点击才继续)。Web 层单独放在
[`codelet/web/`](codelet/web/),不进核心的 ≤3000 行。

## 预置的 profiles 速查

[`.codelet/settings.json`](.codelet/settings.json) 里已经塞好这些
（`--profile <name>` 直接用，记得 `.env` 里填对应 key）：

| profile | provider | base_url | model |
|---|---|---|---|
| `anthropic` | anthropic | (默认) | `claude-sonnet-4-5` |
| `openai-gpt4o` | openai | `api.openai.com/v1` | `gpt-4o` |
| `deepseek` | openai | `api.deepseek.com/v1` | `deepseek-chat` |
| `deepseek-coder` | openai | `api.deepseek.com/v1` | `deepseek-coder` |
| `openrouter-claude` | openai | `openrouter.ai/api/v1` | `anthropic/claude-3.5-sonnet` |
| `siliconflow-qwen` | openai | `api.siliconflow.cn/v1` | `Qwen/Qwen2.5-Coder-32B-Instruct` |
| `moonshot` | openai | `api.moonshot.cn/v1` | `moonshot-v1-32k` |
| `ollama-local` | openai | `localhost:11434/v1` | `qwen2.5-coder` |

`provider` 只有两种值：`anthropic` / `openai`。任何「自称 OpenAI 兼容」的服务都用
`openai`，靠 `base_url` 区分。OpenAI 客户端内部做了 Anthropic ↔ OpenAI 双向翻译
（消息格式、tool schema、tool_calls ↔ tool_use、`is_error` 内联标注），上层
agent loop 完全感知不到差异。

## 配置（settings.json，P4）

分层加载：`~/.codelet/settings.json`（用户级）合并 `./.codelet/settings.json`（项目级，覆盖标量、按 model 合并 pricing、追加 hooks）。
项目里已带示例 [`.codelet/settings.json`](.codelet/settings.json)。

```json
{
  "model": "claude-sonnet-4-5",
  "permission_mode": "ask",
  "max_turns": 30,
  "context_window": 200000,
  "compact_threshold_ratio": 0.75,
  "compact_keep_recent": 4,
  "compact_model": "claude-haiku-4-5",
  "hooks": {
    "PreToolUse":       [{"matcher": "bash", "command": "..."}],
    "PostToolUse":      [{"matcher": "*",    "command": "..."}],
    "UserPromptSubmit": [{"matcher": "*",    "command": "..."}]
  },
  "pricing": {"claude-sonnet-4-5": {"input": 3.0, "output": 15.0}}
}
```

CLI 参数（`--model` `--mode` `--max-turns` 等）优先级 > settings.json > 内置默认。

## Hooks（P4）

三种事件：

| 事件 | 何时触发 | 阻断方式 | 重写方式 |
|---|---|---|---|
| `PreToolUse`       | 工具执行前 | exit code ≠ 0 | stdout JSON `{"tool_input": {...}}` |
| `PostToolUse`      | 工具执行后 | 永不阻断（best-effort 日志） | — |
| `UserPromptSubmit` | REPL 收到用户输入时 | exit code ≠ 0 → 抛 `PromptBlocked` | stdout JSON `{"prompt": "..."}` |

每个 hook 是一条 shell 命令，stdin 收到 JSON 事件，30 秒超时。
`matcher` 字段：`"*"`（任意）、精确名（`"bash"`）、逗号列表（`"bash, write_file"`）。

## Context 压缩（P4）

每个 turn 收尾时估算 token，若超过 `context_window * compact_threshold_ratio`：
- 第一条消息（种子任务）保留
- 最后 `compact_keep_recent` 条保留（不会切断 in-flight 的 tool_use / tool_result 配对）
- 中间被一次 Haiku 调用总结成单个 `<conversation_summary>` 块
- 失败时只打印警告，**不会**让主 agent 崩

## Telemetry（P4）

每次 `client.chat()` 返回的 `usage` 自动累计到 `Telemetry`：
- 当轮 / 累计 input + output tokens
- USD 成本（基于 settings.json 中 `pricing` 表，未知模型显示 `n/a`）
- subagent 的 token 也算到同一份 telemetry — 你看到的是真实总开销

REPL 每个 turn 末尾自动渲染面板；可用 `--no-telemetry` 关掉，或 `/usage` 手动查看。

## Skill 系统

放在 `./.codelet/skills/*.md`（项目级，覆盖用户级）或 `~/.codelet/skills/*.md`（用户级），格式：

```markdown
---
name: python-review
description: Audit Python files for unused imports, TODO comments
triggers: [review, lint]
allowed_tools: [glob, grep, read_file]
---
# body...
```

启动时只把 `name: description` 单行索引塞进 system prompt（最多 30 条、每行
≤80 字符），完整 body 由 `skill` 工具按需取出 — 冷上下文不会因 skill 多而膨胀。
项目里已带一个示例 [`.codelet/skills/python-review.md`](.codelet/skills/python-review.md)。

## SubAgent + 并行 Task

模型可以发 `task` 工具调用把焦点子任务派给 subagent。subagent 拥有：

- 独立 `ConversationContext`（父消息**不**会泄漏）
- 独立 system prompt（SubAgent 模板，含同一份 skill 索引）
- 共享父级 `ToolRegistry` / `LLMClient` / `SkillIndex`（按引用，零开销）
- 可选 `allowed_tools` 白名单收窄能力面
- 递归深度硬上限 = 2（超出直接返回 `is_error` 字符串，不做 LLM 调用）
- 每个 subagent turn 上限 = 8

一个 turn 内多个 `task` 调用会被父循环的 `asyncio.gather` 并发派发，结果按
原 `tool_use` 顺序回填。

## 流式输出（P6）

REPL 里每个 turn 的模型输出实时逐 token 打印，工具调用前显示等待 spinner。
eval / subagent / 一次性 prompt 场景默认关流式（拿完整结果更省事）。

## 评测（P6）

两套执行式评测,都复用 CLI 的 LLM 配置（`.env` + `settings.json` + `--profile`）：

- **mini eval 框架** [`evals/`](evals/)：每个 task 描述「起始工作区 + prompt + 检查项」，
  runner 建目录、跑真实 agent、执行检查（`cmd` 退出码 / 文件 `contains` 断言），
  输出 pass-rate / token / 成本。全通过才 exit 0（可挂 CI）。

  ```bash
  python -m evals.runner                 # 跑 evals/tasks 下全部
  python -m evals.runner --filter slug --profile deepseek
  ```

- **SWE-bench Lite 适配器** [`evals/swebench/`](evals/swebench/)：两阶段流程。stage-1
  在本地克隆仓库 @ base_commit、跑 agent、`git diff` 生成 `predictions.jsonl`（原生
  可跑）；stage-2 用官方 harness 在 Docker 里评分。

  ```bash
  python -m evals.swebench.run_swebench --gold --limit 5   # 无 LLM 冒烟测全链路
  python -m evals.swebench.run_swebench --limit 1
  ```

  > ⚠️ 官方 `swebench` 评分 harness 在 Windows 原生跑不了（`import resource`）。
  > 走 **WSL2** 或 **sb-cli 云评分**，详见 [`evals/swebench/README.md`](evals/swebench/README.md)。

## 测试

```bash
pytest -q          # 149 passed
```

## 目录结构（P1–P6）

```
codelet/
├── agent_loop.py        # async 主循环 + 并行 dispatch + hooks + 压缩 + telemetry + diff 确认 + 流式
├── cli.py               # Rich REPL + .env/settings 加载 + slash 命令 + /resume + spinner
├── config.py            # Config + (provider, model, base_url, api_key) 四元组 + profile_name
├── context.py           # 消息缓冲 + CLAUDE.md + token-aware 压缩
├── permissions.py       # 2 层权限门
├── settings.py          # settings.json 分层加载 + .env 加载 + profile resolver
├── system_prompt.py     # 主模板 + 工具列表 + 模式说明 + skill 索引
├── telemetry.py         # token / 成本累计 + Rich 面板（含定价表）
├── hooks/
│   └── runner.py        # PreToolUse / PostToolUse / UserPromptSubmit 执行器
├── llm/
│   ├── base.py              # LLMClient ABC
│   ├── anthropic_client.py  # 原生 Anthropic（sync）
│   ├── openai_compat.py     # OpenAI 兼容（DeepSeek/Ollama/任何 OpenAI 协议中转站）
│   └── factory.py           # 按 provider 选择
├── persistence/
│   └── session.py       # JSON 快照（原子写）+ /resume + 列表查询
├── skills/
│   └── loader.py        # frontmatter md 解析 + 项目/用户合并（项目优先）
├── slash/
│   └── loader.py        # 用户 slash 命令模板（带 {args} {1} {2} 占位）
├── subagent/
│   └── runner.py        # SubAgentSession（深度上限、共享 hooks/telemetry）
└── tools/
    ├── base.py              # Tool ABC + Registry + async aexecute + preview_diff
    ├── bash_tool.py
    ├── file_read.py / file_write.py / file_edit.py    # 后两者带 preview_diff
    ├── glob_tool.py / grep_tool.py
    ├── web_fetch.py         # httpx async 抓 URL，HTML→text
    ├── skill_tool.py        # 按需取 skill body
    ├── task_tool.py         # 派发 subagent
    └── todo_write.py        # 内存 todo + Rich 表格渲染

evals/                       # P6 执行式评测
├── runner.py            # task → 建工作区 → 跑 agent → 检查 → pass-rate/token/成本
├── tasks/*.yaml         # 本地 task（起始文件 + prompt + 检查项）
└── swebench/
    └── run_swebench.py  # SWE-bench Lite stage-1（克隆 → 跑 agent → predictions.jsonl）
```

## Session 持久化（P5）

每个 turn 结束自动写到 `~/.codelet/sessions/{id}.json`（用 `tmp + os.replace`
保证原子性，被中断不会留半成品）。
- `/sessions` 列出最近 30 条
- `/resume <id>` 在当前 REPL 里恢复（消息、todo、telemetry 从快照重建；工具/hook/skill
  来自当前 settings 而不是快照 — 改了 settings 重启就会生效）
- `--no-persist` 关掉自动保存
- `--resume <id>` 启动即恢复

## Diff 预览（P5）

ASK 模式下，`write_file` / `edit_file` 在真正执行前会：
1. 调 `tool.preview_diff(params)` 生成 unified diff
2. 用 Rich 高亮渲染
3. 提示 `apply this change? [y/N]`

拒绝时返回 `is_error=True` 的 tool_result，模型可以继续基于这个反馈调整。AUTO/PLAN
模式不弹确认；subagent 永远不弹（用户看不到提示）。

### 并行 dispatch 关键约束（P2）

- 单个 turn 拿到 N 个 `tool_use` 时，`asyncio.gather` 并发跑，结果按
  `tool_use_id` 收集后**严格按 LLM 原始顺序回填**（Anthropic API 强制要求）
- 任一工具抛异常都会被 `return_exceptions=True` 隔离，转成 `is_error=True`
  的 tool_result，兄弟工具继续
- sync 工具（实现 `execute`）由 `Tool.aexecute` 默认通过 `asyncio.to_thread`
  自动包装 —— 业务方零改动
- async-native 工具（如 P5 WebFetch）直接覆盖 `aexecute`

## License

MIT。
