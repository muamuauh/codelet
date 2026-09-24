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

`codelet/plugins/builtin/` 下有五个参考实现。它们**绝不会被自动发现** —— 必须在
settings.json 里点名启用：

```json
{"plugins": {"enabled": ["sandbox", "rag", "evolve", "memory", "router"],
             "config": {"sandbox": {"image": "python:3.12-slim", "network": false},
                        "rag": {"inject": false, "top_k": 3},
                        "evolve": {"dir": ".codelet/evolved"},
                        "memory": {"dir": ".codelet/memory", "user_dir": "~/.codelet/memory"},
                        "router": {"llm": false}}}}
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
- **memory** —— 跨会话记忆：提供 `memory` 工具（view / write / delete）和 `/memory` 命令，把记忆的
  索引放进系统提示词。见下方[记忆](#记忆下一次会话该知道的少量事实)。
- **router** —— 意图路由：把「只是在问」的一轮设成只读。见下方[意图路由](#意图路由只是在问的时候不改文件)。

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

## 记忆：下一次会话该知道的少量事实

codelet 里有三种「记住」，容易混：

| | 记什么 | 活多久 | 在哪 |
|---|---|---|---|
| 会话历史 | 整段对话 | 为了 `/resume` | `persistence/session.py` |
| 压缩摘要 | 当前会话的有损摘要 | 只在本次会话里 | `context.py` |
| **记忆** | 少量有类型的事实 | **跨会话** | 这个插件 |

**存储。** 一条记忆一个 markdown 文件，带一小段 front matter（`key` / `type` / `description` /
`source` / `updated`），正文写事实本身、为什么成立、怎么用。`type` 是 user（用户是谁、怎么工作）/
feedback（对做法的纠正或偏好）/ project（关于这个代码库的事实）/ reference（外部资料在哪）。
`type: user` 默认存到用户目录（所有项目共享），其余存在项目的 `.codelet/memory/`（默认被 git 忽略）。

**几个取舍：**

- **索引每次从文件现算，不单独存**，所以不会和文件不一致；手工编辑、删除文件就是修改记忆的正当方式。
- **系统提示词里只放索引**，一条一行（和 skill 一样）；正文用 `view` 按需读。所以 `description`
  必须写事实本身（「Tests are named check_*.py here」），而不是标题（「Test naming」）——下次会话
  不点开时，只看得到这一行。
- **索引在会话开始时读一次，写入后不重建**：系统提示词保持稳定，而本次会话里模型本来就知道自己
  刚写了什么。
- **写入按 key 覆盖**，纠正会替换旧条目，而不是再堆一条近似的；key 已存在时沿用它原来的目录。
  ASK 模式下每次写入和删除都走和 `write_file` 一样的 diff 审批（工具声明 `confirm_in_ask = True`）。
- **像密钥的内容拒绝写入**：记忆是明文文件，项目的记忆目录可能被提交。

**评测**（[evals/memory/two_session.py](../evals/memory/two_session.py)）：5 个场景，每个是两次会话
——会话 1 做一件事，然后用户纠正一条项目约定（测试文件命名、报告放哪、文件头、用 logging 不用
print、只用标准库）；会话 2 是一个全新的 agent，做一件适用这条约定的事，由脚本判定是否照做。会话 2
在两份工作区里各跑一次：保留会话 1 的文件（贴近真实），和重置成原始文件（只有记忆能带过去）。
claude-haiku-4-5，每个场景重复 2 次：

| | 保留会话 1 的文件 | 干净工作区 | 会话 1 存下了记忆 |
|---|---|---|---|
| 不开记忆 | 2/10 | 1/10 | — |
| 开记忆（第一版提示） | 8/10 | 7/10 | 6/10 |
| **开记忆（最终版）** | **9/10** | **10/10** | **10/10** |

第一版的瓶颈不在「想起来」而在「想到要记」：存下了的场景，会话 2 全部照做；两条纠正
（用 logging、只用标准库）agent 改完代码就结束了，一条都没记。把「用户刚纠正你时，先记下来再去改」
写进 `memory` 工具自己的描述（而不只是系统提示词末尾的规则）之后，存下率从 6/10 到 10/10。
局限：场景少、合成的、只用了一个模型；「不开记忆」时会话 1 的文件本身也能带出约定，这正是要分两种
工作区跑的原因。

## 意图路由：只是在问的时候不改文件

每轮用户输入先分成五类：问答 / 规划 / 不明确 / 修改 / 执行命令。前三类这一轮**只读**：
没声明自己只读的工具一律拒绝，拒绝信息让模型「说明要改什么、请用户确认」；用户回一句
「好的 / 改吧 / go ahead」，下一轮就恢复。修改和执行命令两类，和没有路由时完全一样。

**只在有把握时才收紧。** 规则判不了的一轮保持原样，所以分错最多多一轮确认，永远不会比
没有路由更弱。默认只用规则（免费、即时、可测）；`"llm": true` 时，规则判不了的那几轮再问
一次 `compact_model`。

**先修了一个漏洞。** 要做「只读」，先得让只读真的只读：原来 PLAN 模式拦截的是一张写死的
名单（`bash` / `write_file` / `edit_file`），于是插件带来的会写的工具——`sandbox`（工作区
以可写方式挂进容器）、`create_tool`、`memory`——在一个叫「只读」的模式里畅通无阻。现在改成
**白名单**：工具自己声明 `read_only`（或按调用判断的 `is_read_only(params)`：`bash` 只放行
单条、不带重定向和串联的只读命令，`memory` 只放行 view），没声明的一律当作会写。这条对 PLAN
模式和路由的只读轮同时生效；副作用是 PLAN 模式现在能跑 `ls`、`git status` 这类命令了。

**评测**（[evals/intent/](../evals/intent/)）：

分类：160 条请求（自己写、自己标注——本地会话里没有真实数据），五类各 32 条，每类一半做
开发集（用来改规则）、一半做测试集（只用来报数）。最看重两个数：「误拦」——该改的被设成只读；
「保护」——该只读的真的只读了。

| | 误拦 | 保护 | 规则判不了 |
|---|---|---|---|
| 规则，开发集（按它改过规则） | 1/32 | 46/48 | 3/80 |
| **规则，测试集** | **2/32** | **41/48** | 7/80 |
| 规则 + 模型兜底，测试集 | 2/32 | 46/48 | 0/80（调了 7 次模型） |

照开发集改规则时，开发集的保护率从 27/48 升到 43/48，测试集只从 28/48 升到 32/48，误拦还多
了一条——这是只用测试集报数的原因。测试集上的两条误拦：「Could you make load_settings return a
dataclass?」被当成问答，「看看 CI 为什么失败」被当成不明确。

端到端（[unasked_edits.py](../evals/intent/unasked_edits.py)）：一个带 bug 的小仓库，8 个提问 /
规划类请求（好几个在诱导「顺手修了」）和 4 个真要改的请求，claude-haiku-4-5，各跑 2 次：

| | 只是提问时文件被改了 | 真要改时改成了 |
|---|---|---|
| 不开路由 | 1/16 | 8/8 |
| 开路由 | 0/16 | 8/8 |

**结论和取舍：** 收益是真的但不大——这个模型在这组题上很少擅自改文件（16 次里 1 次）；路由的
价值在于**保证**：一轮被判成提问，机制上就改不了，而且不妨碍真要改的请求。按「收益不明显就不
默认开启」的原则，路由保持默认关闭。「Why would divide(1, 0) crash?」这种带排障词的提问会被当成
执行命令放行，这是规则的已知局限。

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
| 每轮策略 | `ctx.on_turn(fn)` | 意图路由：决定这一轮是否只读 | 已实现 |
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
- `Tool.confirm_in_ask`：工具声明为 True 时，ASK 模式下只要它的 `preview_diff` 返回了 diff，就要等
  用户批准——插件工具不必让核心知道自己的名字就能接入审批（`memory` 用的就是它）。
- `Tool.read_only` / `Tool.is_read_only(params)`：工具声明自己是否只读。PLAN 模式和路由的只读轮
  只放行声明了只读的工具——白名单，不是黑名单。
- `AgentLoop._decide_turn`：每轮用户输入进来时问一遍插件的 `on_turn` 策略（第一个给出结论的生效，
  出错的跳过），结果在 `_dispatch_one` 里对整轮生效。

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
