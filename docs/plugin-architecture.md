# 插件架构（P8 / P9）

## 状态：已实现

已建成（`codelet/plugins/`）：`register_tool`（同名注册会覆盖核心工具）、
`add_system_prompt_section`、`on_user_prompt`（prompt 中间件）、`wrap_tool`
（异步的工具执行中间件）、`register_command`（`/name` 斜杠命令，返回一行状态字符串，
CLI 与 Web GUI 都已接好），以及 `ctx.host`（指向运行中的 `AgentLoop`，供需要在会话中途
改动系统的插件使用）。

发现渠道是 entry points（`codelet.plugins`）+ `./.codelet/plugins/*.py` +
`~/.codelet/plugins/*.py`，由 settings.json 里的 `plugins` 块筛选与排序；任何插件出错都被
隔离，不会拖垮 agent。**子 agent 会继承**父级的 tool/prompt 中间件、system-prompt 段和命令
（插件注册的工具则通过复制的 registry 一并带过去）。

尚未实现：provider 注册（`register_provider`）与插件的 `shutdown` 生命周期钩子。

### 内置插件（随 codelet 发布，但只在显式启用时加载）

`codelet/plugins/builtin/` 下有三个参考实现。它们**绝不会被自动发现** —— 必须在
settings.json 里点名启用：

```json
{"plugins": {"enabled": ["sandbox", "rag", "evolve"],
             "config": {"sandbox": {"image": "python:3.12-slim", "network": false},
                        "rag": {"inject": false, "top_k": 3},
                        "evolve": {"dir": ".codelet/evolved"}}}}
```

- **sandbox** —— 新增一个**独立的** `sandbox` 工具，每条命令都在一次性 Docker 容器里执行，
  工作区挂载到 `/workspace`，默认无网络。核心的 `bash`（宿主机）原样保留，由模型按命令自行
  选择用哪个。真正的容器级隔离；需要 Docker；每次调用都是全新容器（有依赖关系的步骤请用
  `&&` 串起来）。
- **rag** —— 提供 `search_docs(query, k)` 工具（对工作区文件分块做 BM25，纯 Python，不调用任何
  embedding API）、`/rag` 命令（重建索引并报告统计），以及在 `"inject": true` 时启用的 prompt
  中间件（把最相关的片段拼到每条消息前）。工作区切换时索引会自动重建。
- **evolve** —— 自进化：提供 `create_tool` 元工具，让 agent 在对话中途自己写一个新工具并热加载
  进当前会话。见下方[自进化](#自进化agent-自己长出工具)。

## 自进化：Agent 自己长出工具

**evolve** 插件把插件系统闭环了：不再由人来写插件文件，而是 **agent 自己**在撞上能力缺口时写一个，
并且立刻生效。

**流程。** 插件注册唯一一个元工具 `create_tool`，它绑定在运行中的 `AgentLoop` 上（通过
`ctx.host` 拿到）。当模型带着 `name` / `description` / `parameters`（一个 JSON-schema 对象）/
`code`（一段以 `params` 字典为输入的 Python 函数体）调用它时：

1. **校验名字**：必须是全新的 snake_case 标识符；核心工具受保护，不允许被临场发挥覆盖掉。
2. **渲染成一个完整、可读的插件模块** —— 一个包住所写函数体的 `Tool` 子类，加一个模块级
   `PLUGIN` —— 并先 `compile()` 一遍，把语法错误挡在落盘之前。
3. **写入 evolved 目录**（默认 `.codelet/evolved/<name>.py`）。
4. **调用 `host.activate_plugin_file(path)`**：加载这一个文件并应用到**运行中的** registry，
   合并它带来的 prompt 段 / 中间件 / 命令，然后重建 system prompt —— 于是这个新工具在 agent
   的**下一轮**就能调用，无需重启。

启动时插件会重新加载 evolved 目录下的每个文件，所以自建的工具**跨会话持久**。`/evolve` 可以列出
它们。每个 evolved 文件都是一个普通的插件模块，你可以随时阅读、修改、挪进 `.codelet/plugins/`
或直接删掉。

在 Web GUI 里侧栏会实时刷新：任何一轮如果改变了工具集，服务端就推送一帧新的 `tools`，于是刚造出来
的工具（连同它的启用/禁用复选框）立即出现，不需要重连。

**安全边界。** 自己写的代码是在进程内、以完整权限运行的，所以这个插件是**选择性开启**的
（内置，但只在 `plugins.enabled` 里点名时才加载），默认关闭。在 **ASK** 模式下，`create_tool`
会把**生成的源码**通过常规的 diff 审批流程先展示出来（它在 `_DIFF_CONFIRM_TOOLS` 里），由人过目
之后才会写入并加载。evolved 目录默认被 git 忽略 —— 想长期保留某个工具，请有意识地把它复制进仓库。
出问题的 evolved 工具是被隔离的：语法错误会回报给模型让它自己改，运行时异常会被捕获并作为工具错误
返回，启动时导入失败的文件只会打一条警告然后跳过，不会让 agent 崩溃。

```
用户： "统计一下 README.md 的词频"
Agent → create_tool(name="word_freq", parameters={... "path" ...},
                    code="import collections, re; ...; return ...")
        ← "Created and activated tool 'word_freq'. It is now available…"
Agent → word_freq(path="README.md")   # 下一轮直接调用
```

## 最小可用插件

丢一个文件到 `.codelet/plugins/audit.py` 即可：

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

PLUGIN = AuditPlugin()   # 加载器找的就是模块级的 PLUGIN
```

在 settings.json 里启用/编排（可选 —— 没有 `enabled` 时，被发现的插件默认全部加载）：
`{"plugins": {"enabled": ["audit"], "config": {"audit": {}}}}`。

## 插件能贡献什么

插件就是一个小对象，拿到 `PluginContext` 之后按需注册下面这些：

| 贡献 | `ctx` 上的 API | 用来做什么 | 状态 |
|---|---|---|---|
| 工具 | `ctx.register_tool(tool)` | RAG 的 `search_docs`、sandbox 执行器 | 已实现 |
| 覆盖工具 | 同上（同名即替换） | 用受限实现替掉某个核心工具 | 已实现 |
| 工具中间件 | `ctx.wrap_tool(fn)` | 审计、脱敏、执行路由 | 已实现 |
| Prompt 中间件 | `ctx.on_user_prompt(fn)` | RAG 上下文注入、护栏 | 已实现 |
| System-prompt 段 | `ctx.add_system_prompt_section(text)` | 把插件能力讲给模型听 | 已实现 |
| 斜杠命令 | `ctx.register_command(name, fn)` | `/rag`、`/evolve` | 已实现 |
| 插件配置 | `ctx.config` | 来自 `settings.json` 的该插件配置 | 已实现 |
| 运行中的 loop | `ctx.host` | 会话中途热加载（自进化用它） | 已实现 |
| LLM provider | `ctx.register_provider(name, factory)` | 接新的模型后端 | **未实现** |

```python
# codelet/plugins/base.py
class Plugin(Protocol):
    name: str
    def setup(self, ctx: "PluginContext") -> None: ...
```

> 注意：早期方案里设想过一个 `shutdown()` 生命周期钩子（用来销毁容器、释放索引），
> 目前**没有实现**；需要清理的插件请自己在工具内部处理。

## 发现与启用

`codelet/plugins/loader.py` 里的 `discover_plugins()` 按顺序收集：

1. **Entry points** —— `importlib.metadata.entry_points(group="codelet.plugins")`，
   也就是可以 `pip install` 的第三方插件。
2. **本地文件** —— `.codelet/plugins/*.py`（项目级）和 `~/.codelet/plugins/*.py`（用户级），
   每个文件暴露一个 `PLUGIN` 对象 —— 和 skills / commands 的加载方式一致。

随后 `apply_plugins()` 用 `settings.json` 做筛选和排序：

```json
{"plugins": {"enabled": ["sandbox", "rag"], "config": {"sandbox": {"image": "python:3.12"}}}}
```

`enabled` 如果存在，就同时是**白名单和加载顺序**；不存在时，被发现的插件全部加载。内置插件是
例外：它们只在 `enabled` 里被点名时才加载（一个 sandbox 悄无声息地把 `bash` 换掉会很难受）。
未知的、加载失败的插件只打一条警告然后跳过 —— 绝不让 agent 崩溃（和 hooks / 压缩是同一套契约）。

## 与核心的集成点（改动都很小、很局部）

- `AgentLoop.__init__`：registry 建好之后调用 `apply_plugins(self.registry, ..., host=self)`，
  它会逐个执行 `plugin.setup(ctx)`；贡献的工具直接落进 `self.registry`，中间件和 prompt 段则
  挂在 loop 上。
- `AgentLoop.activate_plugin_file(path)`：加载单个插件文件并应用到**运行中**的 loop，然后重建
  system prompt。这是自进化的入口。
- `build_system_prompt`：接收额外的 `sections` 并追加进去（RAG 能力说明、sandbox 提示等）。
- `run_async` 的 prompt 路径：在把用户消息入队之前跑一遍 `on_user_prompt` 中间件
  （相当于 shell `UserPromptSubmit` hook 的 Python 版）。
- `_dispatch_one`：把 `tool.aexecute` 包进 `wrap_tool` 中间件链 —— 审计器在这里观察执行。
  原有的 shell hooks 保持不变；插件中间件是它在进程内更强的兄弟。
- 子 agent 通过 `inherit_plugins_from(parent)` 按引用继承父级已应用的插件（和 skills / hooks 一样）。

## 两个内置插件是怎么落地的

**sandbox。** `register_tool` 一个名为 `sandbox` 的独立工具（**不是**覆盖 `bash`），内部用
`docker run --rm` 起一次性容器，工作区绑定挂载到 `/workspace`，默认 `--network none`，复用了
SWE-bench 那套已经跑通的 Docker 管路。`add_system_prompt_section` 告诉模型这个工具存在、以及
"每次调用都是全新容器"这一约束，由模型自己决定哪条命令该进沙箱。配置项：镜像、网络开关。

**rag。** `register_tool` 一个 `search_docs(query, k)`，在本地索引（对文件按行块切分后做 BM25，
纯 Python，无外部服务）上检索；`register_command("rag", ...)` 提供 `/rag` 重建索引并报告统计；
`on_user_prompt` 中间件在开启 `inject` 时把 top-k 片段作为上下文前置（有长度上限）。索引与
检索后端都在插件配置里，核心一点都不用知道。

## 测试与安全边界

- **单元测试**：一个假插件同时注册工具 + prompt 段 + `wrap_tool`，验证 loop 三者都生效；一个
  故意抛异常的插件会被跳过并只打警告。
- **隔离性**：中间件顺序是确定的；插件抛异常绝不会杀掉一整轮对话。
- **信任边界**：插件运行的是任意 Python 代码（和 hooks 一样）。启用必须在 `settings.json` 里显式
  写明，发现机制按项目选择性开启 —— 这条边界要在文档里讲清楚。自进化把这条边界推得更远，所以它
  额外加了 ASK 模式源码审批和核心工具保护，见上文。

## 暂不做的事

插件市场 / 版本管理，以及跨插件的依赖解析。第三方插件保持静态（启动时加载）；唯一的动态路径是
**自进化**（evolve 插件把 agent 自己写的工具热加载进它专属的 evolved 目录，见上文），这是有意
限定在那个选择性开启的子系统里的，而不是给任意插件开放通用热重载。

---

## 附：原始分期计划（历史记录）

下面是 P8 动工前写下的分期，留作对照 —— 第 1–3 步已完成，第 4 步（把参考插件拆成独立的
`codelet-sandbox` / `codelet-rag` 发行包）没有做，两个插件最终作为内置插件留在了主仓库里。

1. `plugins/base.py`（`Plugin`、`PluginContext`）+ `plugins/loader.py`（entry points + 本地目录 +
   settings 白名单）+ `apply_plugins`。先只做工具和 prompt 段，附一个最小示例插件和测试。
2. 中间件：把 `on_user_prompt` 和 `wrap_tool` 两条链接进 loop。
3. Provider 注册 + 斜杠命令注册；子 agent 继承。（provider 注册最终未做）
4. 参考插件：`codelet-sandbox`、`codelet-rag` 拆成独立可选包（各自的 `[sandbox]` / `[rag]`
   extras），验证 entry-point 这条路。（未做）
