# codelet 使用说明

> Windows + miniconda 环境下的实操指南。环境配置见 [README.md](README.md)。

## 1. 启动前（每次开新终端）

```powershell
conda activate codelet      # 看到 (codelet) 前缀才算成功
$env:PYTHONUTF8="1"                # 防中文乱码，建议加到系统环境变量一劳永逸
```

> 想省事可把 `$env:PYTHONUTF8="1"` 写进 PowerShell profile
> （`C:\Users\<你>\Documents\PowerShell\profile.ps1`），开终端自动设好。

## 2. 三种运行方式

```powershell
# 交互式 REPL（最常用，可连续对话）
python -m codelet

# 一次性提问（问完即退出）
python -m codelet "在当前目录找出所有 TODO 注释"

# 恢复历史会话
python -m codelet --resume <session-id>
```

## 3. 权限模式与思考强度（核心概念）

| 模式 | 行为 | 用法 |
|---|---|---|
| `normal` | 写文件预览 diff 确认，常用安全命令（`git status`/`python` 等）自动放行 | `--mode normal` 或 `/mode normal` |
| `ask`（默认） | 写文件及所有非只读终端命令均弹窗/提示确认 | 安全，适合高敏感修改 |
| `plan` | 只读分析与规划，所有写入类工具硬拦截 | `--mode plan` 或 `/plan <任务>` |
| `auto` | 自动执行写文件与常规命令（仍拦截高危危险命令） | `--mode auto` 或 `/mode auto` |

思考强度可通过 `--effort [auto|off|low|medium|high]`、`/effort` 或 Web 底部 `Think` 选择器随时调节（`auto` 跟随模型默认，非推理模型会自动降级忽略）。

```powershell
python -m codelet --mode normal --effort medium "把所有 print 改成 logging"
python -m codelet.web   # 启动本地三栏 Web 工作台
```

改文件时（`ask` / `normal` 模式）会先弹 **diff 预览**，确认 `y` 才写入；拒绝 `N` 它会根据反馈调整。

## 4. REPL / Web 内置命令

```
/help                               所有命令
/tools  /skills  /commands          查看可用工具 / 技能 / 自定义命令
/models [keyword|refresh]           查看或刷新远程模型目录
/model [id]                         查看或切换当前模型
/mode [normal|ask|plan|auto]        切换四档权限模式
/effort [auto|off|low|medium|high]  调整思考强度
/btw <question>                     独立上下文旁路提问（不污染主对话）
/compact [focus]                    手动压缩上下文（可选保留重点）
/clear  /new                        清空并开启新对话
/plan [task]                        切入只读规划模式
/diff                               查看工作区 Git 变更与 Diff
/cockpit [refresh]                  打开 Cockpit 监控 / 刷新 ZenMux 余额
/install <github-url> [--global]    从 GitHub 安装 Skill 并立即热重载
/export                             导出当前对话为 Markdown
/usage  /cost                       当前会话及跨会话 token / 成本统计
/profile                            当前激活的 LLM (provider, model, base_url)
/todos                              查看任务清单
/sessions  /resume <id>  /save      会话列表 / 恢复 / 手动存档
/quit                               退出
```

`/sessions` 分两段显示，每行以**标题**（该会话的第一句话）开头：

1. **本项目的会话** — 从当前工作目录启动过的会话，记录在
   `./.codelet/sessions.json`（机器本地，已 gitignore）
2. **全局保存的会话** — 所有项目的会话，过多时只列最近 10 条

恢复用 `/resume <id>`（从上面任一段复制 id）。

## 5. 换 / 切 LLM

**改默认** — 编辑 `.env` 三行即可，换任何 OpenAI 兼容服务都只改这里：

```ini
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=deepseek-chat
```

**多个之间切换** — `.codelet/settings.json` 里已预置 `deepseek` / `openai-gpt4o` /
`moonshot` / `ollama-local` 等 profile，填好对应 key 后：

```powershell
python -m codelet --profile deepseek "..."
```

**临时覆盖**（不写文件）：

```powershell
python -m codelet --base-url https://api.deepseek.com/v1 --api-key sk-xxx --model deepseek-chat "你好"
```

优先级：`CLI 参数 > --profile > settings 默认 > .env(LLM_*) > anthropic`

## 6. 扩展能力（进阶）

| 功能 | 放哪 | 说明 |
|---|---|---|
| **自定义命令** | `.codelet/commands/<name>.md` | 用 `/<name> 参数` 调用，支持 `{args}` `{1}` `{2}` 占位（已带 `/audit` 示例）|
| **Skill 技能** | `.codelet/skills/`（支持 `*.md` 与 `<name>/SKILL.md`） | 模型按需加载的专项知识；内置 `skill-creator` 可按描述生成新技能，或用 `/install <github-url>` 直接安装热重载 |
| **Plugin 插件** | `.codelet/plugins/`（`*.py` 或目录技能包） | 上层热插拔功能包，可捆绑 Tools + Skills + Commands + Panels，并支持声明 `settings_schema` 在 **设置 → 插件** 自动生成专属配置表单；内置 `cockpit` 默认启用 |
| **设置中心** | Web / 桌面端左下角齿轮图标 | 集中管理 **通用**（主题、中英双语、窗口关闭行为、Agent/上下文参数）、**模型与接口**（DPAPI 加密密钥与提供商管理）、**插件**（滑动开关与专属设置）及 **关于**（版本与自动更新检查） |
| **Hooks** | `settings.json` 的 `hooks` | 工具执行前后/收到输入时跑自定义 shell 命令，可拦截/改写 |
| **SubAgent** | 模型自动调用 `task` 工具 | 把子任务派给隔离上下文的子代理，可并行 |

## 7. 流式输出 & 反馈

交互式 REPL 默认**流式输出**：等待首个 token 时显示 spinner + 滚动文案（`Pondering… (3s)`），
首 token 到达后文字逐块出现，像打字机一样。

- 自动关闭场景：输出被重定向/管道（非 TTY）、subagent（其文本不直接展示）
- 手动关闭：`--no-stream`，或在 `settings.json` 里设 `"stream": false`
- 推理(thinking)模型（如 qwen3.7-max）会先有一段"思考"停顿（spinner 转圈），思考完答案再流式吐出

```powershell
python -m codelet --no-stream "..."      # 等整段生成完再一次性输出
```

## 8. 常用运维

```powershell
pytest -q                                       # 跑测试
python -m codelet --no-telemetry "..."   # 关掉成本面板
python -m codelet --no-persist "..."     # 不自动存会话
```

---

## ⚠️ Windows 上的已知坑

1. **别用 `conda run`** 跑带中文输出的命令 —— conda 转发输出时会 GBK 崩溃。先
   `conda activate` 再直接跑 `python -m codelet`。
2. **`conda activate` 不生效**（没有 `(codelet)` 前缀）——说明 conda 没在
   PowerShell 初始化过。运行一次 `conda init powershell`，然后**关闭并重开终端**。
3. bash 工具底层是 **cmd.exe**（不是 PowerShell，也没有 `ls`/`grep`）。需要 unix
   命令时让它用 `powershell -Command "..."`，或装 Git 后把 `Git\usr\bin` 加进 PATH。
