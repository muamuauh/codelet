# 面试式 Q&A

围绕本项目可能被问到的问题，按"先架构、后实现细节、再权衡"分组。每个回答都尽量给出**为什么**，并指向相关代码或文档。

不是每个问题都有"标准答案"——很多决策有合理的反向选择。我把当时的取舍记下来，方便后续重新评估。

---

## 一、架构层面

### Q1. 这个项目想解决什么问题？目标和定位是什么？

一个轻量级的 AI 编码助手框架。核心是**异步 agent loop + 工具系统 + 权限门控**，外加这一套之上的工程化扩展：SubAgent / 并行执行 / Skills / Hooks / 上下文自动压缩 / Token 与成本遥测 / 多 LLM provider 支持。

定位：

1. **可读性优先**——核心循环就一个 `agent_loop.py`，每一处设计都能讲清楚为什么这样写
2. **工程化扎实**——131 个测试覆盖关键不变量（顺序保持、上下文隔离、深度上限、原子写、OpenAI 兼容边界）
3. **真能用**——支持 Anthropic / OpenAI / DeepSeek / OpenRouter / SiliconFlow / Moonshot / 本地 Ollama / 任何 OpenAI 兼容的中转站

边界：不做 MCP、不做 worktree 沙盒、不做 5 层权限模型。把复杂度留给它真正属于的地方。

总规模 ~3500 行核心 + ~1500 行测试，对应 5 个清晰的实现 phase。

---

### Q2. 为什么要引入 LLMClient 抽象？直接调 `anthropic.Anthropic()` 不就行了？

第一阶段其实就是直调 Anthropic SDK。但**第一阶段就**引入了 `LLMClient` ABC 作为占位（虽然只有 Anthropic 一个实现），原因有三：

1. **预先把"provider 特定"和"业务"切开**。等到 P5 加 OpenAI 兼容时，agent_loop 一行没动，只在 `factory.build_client` 里多一个 if 分支。如果到 P5 才抽象，就要改 `agent_loop.py` 里所有 `client.messages.create` 的位置。
2. **测试解耦**。所有 agent_loop 测试都用 `ScriptedClient` 喂 LLMResponse，根本不需要 mock SDK。131 个测试秒级跑完。
3. **明确内部消息格式**。我选 Anthropic-shaped 作为内部规范（因为它有结构化的 content blocks），所有 provider 实现都翻译成这个形状。OpenAI 客户端做的就是 Anthropic ↔ OpenAI 双向翻译，agent_loop 完全感知不到。

代价：多了一层间接，但每层只 ~30 行。值。

---

### Q3. 为什么 Tool 的 `execute` 是同步的，但 agent loop 是异步的？

P1/P2 的演化决定的：

P1 时所有工具都是阻塞 IO（subprocess / 文件读写 / regex），写 sync 最自然。P2 加并行时，最简方案是把每个工具丢到 `asyncio.to_thread` 去跑——这样原本写好的 6 个工具一行不改就能并发。

具体设计：

```python
class Tool(ABC):
    def execute(self, params) -> ToolResult:                    # sync 接缝
        raise NotImplementedError("override execute or aexecute")

    async def aexecute(self, params) -> ToolResult:             # loop 调这个
        return await asyncio.to_thread(self.execute, params)
```

- 阻塞型工具（Bash/Read/Write/Edit/Glob/Grep）只实现 `execute`，自动获得线程包装
- 真正异步的工具（WebFetch / Task）直接 override `aexecute`，不浪费线程

替代方案是"全 async，所有工具用 `await asyncio.to_thread` 自己包"——也行，但每个工具都得多写几行样板。我这种"用基类 boilerplate 默认值"的写法把样板集中到一处。

详见 [technical-details.md §11](technical-details.md#11-sync-async-工具桥接)。

---

### Q4. Tool / Skill / SubAgent 这三个概念怎么区分？

这是项目里**最容易混淆**的概念。我的定义：

| 概念 | 是什么 | 副作用 | 怎么调用 |
|---|---|---|---|
| **Tool** | 一次具体执行 | 可能有 | LLM 发 `tool_use` block |
| **Skill** | 程序性知识（"做 X 的步骤"） | 无 | LLM 发 `skill(name=...)` 工具调用，按需取 body |
| **SubAgent** | 隔离 context 的 agent fork | 通过子工具有 | LLM 发 `task(prompt=...)` 工具调用 |

**举例**：用户说"审查这段 Python 代码"。
- 模型可能先调 `skill(name="python-review")` 拿到 procedure
- 然后用 `task(prompt="for each file in [...], list unused imports")` 把活分给若干并行 subagent
- 每个 subagent 内部用 `read_file` / `grep` / `bash` 这些工具

也就是 skill **写「该怎么做」**，subagent **真去做**，tool 是**最底层的执行单元**。

为什么不把 skill 直接塞进 system prompt？因为 skill 多了会污染冷上下文。System prompt 只放 `name: description` 索引（最多 30 条、每行 80 字），完整 body 由 `skill` 工具按需拉取。

---

### Q5. 为什么 SubAgent 深度上限定 2，不是 1 或 3？

权衡：

- **1 层**：父 → 子，子不能再 spawn。简单但会限制有用模式（比如父 → researcher → verifier）
- **2 层**：父 → 子 → 孙。覆盖绝大多数实战模式，且 token 增长仍可控
- **3 层及以上**：递归深度无明显收益，token 成本指数膨胀

经验上 2 层覆盖绝大多数实战模式（研究 → 验证、采集 → 汇总），再深就是过度递归。

实现细节：

- 检查在 `SubAgentSession.run` 入口，**零 LLM 调用就拒绝**（测试 `test_depth_cap_refuses_when_parent_is_already_at_max` 断言 `client.history == []`）
- 配套：单 subagent turn 上限 = 8，单 summary 截断到 4KB
- depth 字段挂在 `ConversationContext` 上，subagent fork 时显式 `parent_depth + 1`

详见 [technical-details.md §3](technical-details.md#3-subagent-深度上限的实现)。

---

### Q6. Hooks 是怎么设计的？为什么用 shell 命令而不是 Python callback？

设计目标：让用户在不改源码的情况下扩展行为。三种事件：

```
UserPromptSubmit  → 改写或阻断用户输入
PreToolUse        → 改写或阻断工具调用
PostToolUse       → 仅日志，不阻断（工具已经跑完了）
```

为什么 shell 而不是 Python：

1. **跨语言生态**：用户的安全审计脚本可能是 Go / Bash / Lua / 任何语言。shell 命令是最低公约数。
2. **进程隔离**：hook 崩溃不能拖死主进程。subprocess + 30s timeout 自动隔离。
3. **零代码加载**：写个 `.codelet/settings.json` 就生效，不需要让用户写 Python 模块、考虑 import 顺序、担心 venv。

代价：每次 hook 调用 fork 一个 subprocess，开销比 in-process callback 高。但 hook 不是热路径，可以接受。

协议：stdin 收 JSON，stdout 可输 JSON 改写，exit code ≠ 0 阻断（PostToolUse 除外）。详见 [technical-details.md §7](technical-details.md#7-hooks-协议设计)。

---

### Q7. 为什么 settings.json 和 .env 分开？合并成一个不行吗？

分开的理由：

- **`.env` 永远不该入版本库**：里面是 secret。`.gitignore` 自动屏蔽。
- **settings.json 应该可入版本库**：里面是可分享的配置（profile 列表、hook 命令模板、price 表）。

让所有人在 settings.json 里写 `api_key: "sk-..."` 然后告诉他们"记得加到 gitignore"——这是教用户写 bug。所以：

- secret → `.env`，git 忽略
- 配置 → `settings.json`，可入库
- 两者通过 `api_key_env` 字段串联：profile 里写 `api_key_env: "DEEPSEEK_API_KEY"`，运行时从 `os.environ["DEEPSEEK_API_KEY"]` 解析

例外口子：profile 里允许直接写 `api_key: "sk-..."` inline，给"我有一个 hosted secret manager 在启动时注入 settings.json"的高级用户用。

---

### Q8. 为什么提供"LLM_* 三元组直接生效"这条快速路径？

最初设计强制每个 provider 都要先在 settings.json profiles 里登记一条，然后 `--profile X`。用户反馈："我就想加个新中转站，干嘛要改两个文件？"

加了 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` env 三元组：**这三个一设就能跑**，不必碰 settings.json，也不必 `--profile`。

```ini
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=deepseek-chat
```

实现位置 [settings.py](../codelet/settings.py) `resolve_profile`：

```python
if name is None:
    if os.environ.get("LLM_API_KEY") or os.environ.get("LLM_BASE_URL"):
        return {"name": "_env", "provider": "openai", ...}  # 合成临时 profile
    name = "anthropic"
```

加新中转站现在的最少操作 = 改 .env 三行。

---

## 二、实现细节

### Q9. 并行工具调度怎么保证 tool_result 顺序与 tool_use 一致？

这是最容易踩的雷。Anthropic API 强制要求两个数组顺序完全一致，错位 → 400 + 晦涩报错。

实现：

```python
async def _dispatch_parallel(self, tool_calls):
    if len(tool_calls) == 1:
        return [await self._dispatch_one(tool_calls[0])]   # 快路径

    coros = [self._dispatch_one(c) for c in tool_calls]
    completed = await asyncio.gather(*coros, return_exceptions=True)

    by_id = {}
    for call, item in zip(tool_calls, completed):
        by_id[call.id] = (
            self._error_result(call.id, f"Dispatcher error: {item}")
            if isinstance(item, BaseException) else item
        )
    return [by_id[call.id] for call in tool_calls]   # ⭐ 按原 tool_calls 顺序
```

`asyncio.gather` 实际上**会**按入参顺序返回（这是 contract），但显式按 `tool_use_id` 重组有两个好处：

1. 防御未来 contract 变化
2. 让"顺序保持"约束在代码里**可见**——新人改代码时一眼能看到这个不变量

测试 (`test_results_preserve_tool_use_order_despite_completion_order`) 用三个不同 sleep 时长（300/100/200ms）的 async 工具，故意让完成顺序与 emit 顺序倒过来，断言 tool_result 按 emit 顺序回填。

---

### Q10. SubAgent 怎么保证父级对话不泄漏？

实现 ([subagent/runner.py](../codelet/subagent/runner.py))：

```python
child = AgentLoop(...)                                       # 共享 client/registry/skill
child.context = ConversationContext(config=child_config)     # ⭐ 全新空 context
child.context.depth = self._parent_depth + 1
child.context.set_system_prompt(_build_subagent_prompt(...)) # SubAgent 模板，不含父级
summary = await child.run_async(spec.prompt)                 # 只有 spec.prompt 进入 child
```

关键点：subagent 的 `context.messages` 是**全新空列表**，`set_system_prompt` 用专门的 SubAgent 模板（不复制父 system prompt 或 CLAUDE.md），父级的 user message 没有任何路径可以进入子级 context。

测试 (`test_subagent_context_does_not_see_parent_history`) 在父 user message 里塞 `SECRET_PARENT_MARKER`，然后遍历每个 subagent 的 chat 调用，断言这个 marker 从不出现在 subagent 的 messages 中。

**早期 bug**：测试一开始用 `"SubAgent" in system_prompt` 来判断是不是 subagent 的 chat 调用。但父级 system prompt 里列出了所有工具描述，TaskTool.description 里有 `"Spawn a SubAgent..."`——这词**也**在父级 system 里。导致测试把父级 chat 误判成 subagent，"看见了泄漏"。修法：换更精确的 marker `"You are a SubAgent"`（只出现在 SubAgent 模板首行）。这是个**测试 false-positive** 的好教训。

---

### Q11. SubAgent 复用父级的 ToolRegistry，但 task 和 todo_write 工具实例不能共享。为什么？怎么解决？

关键代码：

```python
class TaskTool(Tool):
    def __init__(self, parent: AgentLoop):
        self._parent = parent  # ⚠️ 闭包到父级

    async def aexecute(self, params):
        ...
        parent_depth=self._parent.context.depth   # 读父级深度
```

如果 subagent 直接复用父级的 `TaskTool` 实例，subagent 想再 spawn 时读到的是**父级的 depth=0** 而不是自己的 depth=1，深度上限失效。同理 `TodoWriteTool` 持有 store 引用，不重绑会写到父级的 todo store。

解决（[agent_loop.py](../codelet/agent_loop.py) `_wire_dynamic_tools`）：

```python
def maybe(name, factory):
    if allow is not None and name not in allow:
        return
    self.registry.unregister(name)        # ⭐ 剥掉继承来的实例
    self.registry.register(factory())     # ⭐ 重绑到 self

maybe("task", lambda: TaskTool(self))
maybe("todo_write", lambda: TodoWriteTool(self.todo_store))
```

每个 AgentLoop 实例（包括 subagent）init 时都会剥离父级注册的 task/todo，重绑到自己。

为了不污染父级的 registry，subagent runner 在传 registry 给 child 之前做了 shallow copy（[runner.py](../codelet/subagent/runner.py) `_shallow_copy_registry`）：tool 实例本身共享，但容器是新的，subagent 的 unregister 不会影响父级。

---

### Q12. Context 自动压缩为什么用单独的 Haiku 而不是直接截断？

截断策略最简单（"删掉最老的一半"），但有两个问题：

1. **信息丢失**：早期 user 决策（"我们用 Python 3.10"）丢了之后，后续 turn 可能 LLM 又重新问一遍
2. **可能切坏 tool_use/tool_result 配对**：朴素截断不感知消息边界，容易把"工具调用"和它的"结果"切散，下一次 API 调用直接 400

我的实现 ([context.py](../codelet/context.py))：

```python
head = self.messages[:1]                      # 第一条永远保留
tail = self.messages[-keep_recent:]           # 末尾 keep_recent 条永远保留（含完整 tool 配对）
middle = self.messages[1:-keep_recent]
summary = await _summarize(client, middle, config)   # 用 Haiku 总结中段
self.messages = head + [summary_msg] + tail
```

为什么用 **Haiku** 而不是当前用的主模型：

- **便宜**：Haiku 4.5 是 $1/M in / $5/M out，主模型 Sonnet 是 $3/M / $15/M（3-5 倍差）
- **快**：Haiku 延迟更低，压缩不会卡住主循环太久
- **够用**：上下文总结不需要顶级推理能力

`compact_model` 字段在 settings.json 可配。如果用户用的是 OpenAI 兼容 provider，要么 Haiku 不可用，要么把 `compact_model` 设成 `gpt-4o-mini` 或同 provider 的便宜模型。

**异常吞掉**：summarizer 调用可能失败（限流、网络、key 失效）。**主 agent 不能因为压缩失败而崩**：

```python
try:
    await self.context.compact_if_needed(self.client)
except Exception as exc:
    self.console.print(f"[dim yellow][compaction skipped: {exc}][/dim yellow]")
```

详见 [technical-details.md §6](technical-details.md#6-context-压缩的安全切片)。

---

### Q13. OpenAI 兼容客户端有哪些"看似简单实则坑"的细节？

核心翻译规则不复杂，但 6 个边界条件容易踩：

1. **assistant 仅 tool_calls 时 content 必须 None**：不能写 `""`，OpenAI SDK 拒绝
2. **tool_calls.function.arguments 必须 JSON 字符串**：Anthropic 用 dict，转换时 `json.dumps`
3. **空 tools 列表必须省略字段**：`tools=[]` 部分中转会拒，要写成"if tools: kwargs['tools'] = ..."
4. **回程坏 JSON 降级**：小模型偶尔回吐畸形 JSON 作为 arguments，try/except 降级到 `{}`
5. **is_error 内联前缀**：OpenAI 没有这个字段，把 error 信息加 `[ERROR]` 前缀塞进 tool message content
6. **finish_reason 映射**：`tool_calls` ↔ `tool_use`、`stop` ↔ `end_turn`、`length` ↔ `max_tokens`

每条都对应至少一个测试 ([test_openai_compat.py](../tests/test_openai_compat.py))。

详见 [technical-details.md §9](technical-details.md#9-openai-兼容层的边界条件)。

---

### Q14. Session 持久化的原子性怎么实现的？

```python
fd, tmp_name = tempfile.mkstemp(suffix=".tmp", dir=path.parent)
try:
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data))
    os.replace(tmp_name, path)        # ⭐ 原子换名
except Exception:
    os.unlink(tmp_name)               # 异常清理
    raise
```

`os.replace` 在 POSIX 文件系统和 NTFS 都是原子操作——要么旧文件存在，要么新文件存在，不会出现中间状态。即使在 `f.write` 中途 kill -9，主路径上的 session.json 文件还是上次写入的完整版本。

测试 ([test_atomic_write_no_partial_files](../tests/test_persistence.py)) 用 monkeypatch 让 `os.replace` 抛异常，断言：
1. 主 session.json 不出现
2. tmp 文件被清理（`tmp_path.glob("*.tmp") == []`）

为什么不更严格的 `f.flush() + os.fsync()`：那是"断电不丢"语义，需要写存储介质。我们的需求是"重启不丢"，主要担心 kill -9 / Python crash。`os.replace` 已经够。

---

## 三、权衡与未做的事

### Q15. 你的代码很多地方都直接 raise 而不是返回 Optional。这是有意的吗？

是。原则：**Tool execution 内部** 用返回 `ToolResult(is_error=True)` 而不是 raise；**配置 / 协议层** 该 raise 就 raise。

理由：

- 工具异常是常见的（文件不存在、命令失败、超时），让模型看到 `is_error=true` 的 tool_result 后**自我恢复**比让 agent loop 崩好得多。所以工具内部全是 try/except 包成 ToolResult。
- 配置层（profile 解析、settings 加载）异常少见且严重。让 raise 暴露出来才能被 CLI 顶层捕获显示给用户。

例外：`PromptBlocked` 是 raise 的，因为它需要中断当前 turn，REPL 顶层捕获后给用户清晰的"hook blocked"提示。这比让它转成 is_error 然后让 LLM 看到"prompt blocked"更直观——这是用户拒绝行为，不是模型该处理的工具错误。

---

### Q16. 为什么不用 LangChain / pydantic-ai / 现成 agent 框架？

两条理由：

- **可读性**：用框架代码量能从 ~3500 行降到 ~500 行，但读者看到的是"框架的 API 调用"而不是"agent 怎么实现"。本项目目标之一是把每一处设计讲清楚（这是 docs/technical-details.md 存在的意义）。
- **可控性**：遇到 OpenAI 兼容中转站的奇葩行为能直接改翻译层（[openai_compat.py](../codelet/llm/openai_compat.py) 那 6 个边界条件就是真踩出来的）而不是发 GitHub issue 等三个月。框架版本升级也不会让自己的代码烂掉。

副作用：测试不依赖外部框架，跑得飞快（131 个测试 1.4 秒）。

代价：能力面比框架窄，没有 streaming UI、没有 LangSmith 集成、没有内置的 vector store。但项目目标本来就不在那里。

---

### Q17. 为什么 .env loader 自己写而不用 python-dotenv？

20 行代码 vs 一个新依赖。我的实现：

```python
def load_env_files(...):
    paths = [project / ".env", user / ".env"]
    for path in paths:
        if not path.is_file(): continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" not in line: continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key.isidentifier(): continue
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)   # shell env 永远赢
```

不支持 python-dotenv 的高级特性（变量插值、多行值、shell expand）——但用户存 API key 也用不上这些。

**关键 invariant**：`os.environ.setdefault` 而不是 `os.environ[key] = value`——shell 已经设的环境变量永远赢。这样 CI/部署里的 secret manager 和 .env 不会冲突。

---

### Q18. 测试都是用 stub 客户端跑的，没真调过 API。靠谱吗？

不完全靠谱，但**够用**。说说取舍：

**Stub 测试覆盖了什么**：
- 协议级正确性（请求格式、响应解析、错误处理）
- 业务逻辑（depth cap、并行顺序、压缩条件、hook 路径）
- 边界条件（坏 JSON、空 tools、is_error 前缀）

**Stub 测试不能覆盖**：
- 真实 API 的速率限制、超时行为
- provider 实际响应格式的奇葩之处（"我们文档说支持但其实回 500"）
- token 计数的精确性（我们用估算）

**为什么不补集成测试**：

1. 钱：每跑一次 CI 烧 token
2. 不稳定：API 限流 / 网络抖动让 CI 经常红
3. 慢：每个测试至少几百毫秒
4. 安全：CI 跑测试需要的 API key 怎么管理？

工程实际：协议级 stub 测试 + 手动跑一次真实 demo 验收。生产化需要 staging 环境的 smoke test 套件，本项目暂不需要。

---

### Q19. 哪些功能你考虑过但没做？为什么？

主要被砍掉的：

1. **MCP (Model Context Protocol) 支持**：MCP 是 Anthropic 定义的标准，让外部进程暴露工具。实现复杂（stdio JSON-RPC 长连接），生态还不成熟，普通用户用不到。如果将来加，会作为新的 `Tool` 子类（"MCPTool"）让现有架构吞下。

2. **Streaming UI**：LLM 边产 token 边显示。Anthropic 和 OpenAI 都支持 SSE。没做的原因：
   - 当前的 Rich 行为已经够清晰（每个 tool 状态一行）
   - 实现成本不低（要改 LLMClient + agent_loop 渲染）
   - 不影响功能，只影响用户感受

3. **Worktree 沙盒**：让 subagent 在独立的临时目录里跑，避免污染主仓库。Windows 上 worktree 行为不一致，且大多数情况 subagent 只读不写。

4. **Plan 模式预先生成 plan.md**：让 plan 模式产出可 review 的计划文件再执行。当前 PLAN 模式只是禁用 write tools，简化处理；要做完整版需要新增"plan 阶段 → 用户审阅 → 切到 auto/ask 执行"的状态机。

5. **OAuth / API key 自动刷新**：本地工具用静态 key 就够。

6. **多 turn streaming + 并行 agent group + RAG**：超出当前定位的范围，这些更适合作为上层应用基于本框架去构建。

每个砍掉的特性都对应一个 GitHub issue 在脑子里：能讲清楚为什么不做，将来要做的话从哪儿改。

---

### Q20. 如果让你从零再做一次，会改什么？

1. **更早引入 telemetry**：P4 才加遥测，前面的 phase 没法量化"agent 跑多花钱"。如果 P1 就把 telemetry 写进来（哪怕是简单 token 计数），后续 phase 的优化就有依据。

2. **Tool 的 sync/async 用 Protocol 而不是 ABC**：现在 `execute` 默认 raise NotImplementedError 是个 hack。用 `typing.Protocol` 配两种 Tool 类型（SyncTool / AsyncTool）会更干净，但 mypy/runtime 行为有 gap，没下决心。

3. **更少的 dataclass 字段，更多的 `**kwargs` 传递**：现在 Config 字段越来越多，每加个 phase 就涨。其实可以保持 Config 只有"必须在启动时定的"字段，剩下的（hook 配置、价格表）走 settings dict 直传到 HookRunner / Telemetry。

4. **早写 docs**：这套架构图、流程图、技术细节文档应该在 P1 就开始写——边实现边记理由。等 P5 之后再回头补，很多决策的细节已经模糊了。

5. **测试组织**：所有测试现在扁平在 `tests/`，按文件分。规模再大就得按 phase 或按 layer 分子目录。

但**主要决策**——LLMClient 抽象、async 桥接、subagent 隔离、压缩策略、profile 三元组——都站得住，不会改。

---

### Q21. 这个项目你最满意和最不满意的地方分别是什么？

**最满意**：

- **OpenAI 兼容层那 6 个边界条件**。每一条都有 stub 测试盯着，未来踩坑直接看测试就知道哪个不变量破了。这是"工程化"的真本钱。
- **"加新中转站只改三行 .env"**。这是用户反复反馈过、专门重构出来的 UX。从 P5 初版的"必须先在 settings.json 里登记 profile"到最后版本的"`LLM_*` 三元组直接生效"，是个有价值的 UX 演进。
- **subagent 深度上限 + 零 LLM 拒绝**。`client.history == []` 这条断言让我能放心说"恶意 prompt 不会让 fork bomb"。

**最不满意**：

- **`Tool.execute` 默认 raise NotImplementedError 是 hack**。理论上更干净的方案是 Protocol，但 Python 类型系统在这点上不如 TypeScript 顺手。
- **session 恢复不还原 telemetry 历史**。当前只存累计值，重新启动后丢失"per-turn 历史"。要补就得序列化整个 turns 列表，没找到优雅做法。
- **Hooks 没加 metric/timing**。多个 hook 串行跑时如果某个慢，用户只能盯日志猜。一个简单的"hook 耗时分布"telemetry panel 能救命，但 P4 时没想到。
- **Skill 的 allowed_tools 字段目前没强制**。skill frontmatter 里写了 `allowed_tools` 但只是个 hint，subagent 不会真按这个收窄。要做的话要把它和 task tool 的 `allowed_tools` 参数串起来。

---

## 四、扩展性

### Q22. 如果要加一个新的 LLM provider（比如 Cohere 或 Gemini），需要改哪些地方？

最少改动路径：

1. 写 `llm/cohere_client.py`，实现 `LLMClient` ABC，做 Cohere 风格 ↔ Anthropic-shaped 的双向翻译（参考 [openai_compat.py](../codelet/llm/openai_compat.py)）
2. 在 `LLMProvider` enum 加 `COHERE = "cohere"`
3. `factory.build_client` 加个分支
4. settings.json 里加一个 profile

agent_loop / context / 任何工具都不用动。

如果新 provider 自称"OpenAI 兼容"（很多新模型都是），直接复用 `OpenAICompatClient` 加 profile 就行，零代码。

---

### Q23. 怎么加一个 streaming UI？

修改路径：

1. `LLMClient` ABC 加 `async def chat_stream(...) -> AsyncIterator[Event]`
2. `AnthropicClient` 用 `client.messages.stream()`，`OpenAICompatClient` 用 `stream=True` 参数
3. `agent_loop._call_llm` 改成 streaming 模式：逐 chunk 渲染、累积 raw_content、最后 yield 完整 LLMResponse
4. Rich 用 `Live` context manager 实现"原地刷新"

复杂度：~200 行新代码 + 改 telemetry 让它累 chunk usage。值不值得做：看用户反馈优先级。

---

### Q24. 怎么把这个项目变成 SDK / 库（去掉 CLI）？

整理一下边界：

- **核心可复用**：`AgentLoop`, `LLMClient`, `Tool`, `ToolRegistry`, `HookRunner`, `Telemetry`, `SkillIndex`, `SubAgentSession`, `SessionStore`
- **CLI-only**：`cli.py`, `BANNER`, slash command 处理
- **可选耦合**：`load_env_files`, `load_settings`, `resolve_profile` —— 这些是 CLI 的便利，库用户应该能跳过

最少改动：

```python
from codelet import AgentLoop, Config
from codelet.llm import AnthropicClient

agent = AgentLoop(
    config=Config(model="claude-sonnet-4-5", api_key="sk-..."),
    client=AnthropicClient(api_key="sk-..."),
)
result = await agent.run_async("hello")
```

这本来就能跑（CLI 只是 builder + REPL）。我会把 `codelet/__init__.py` 的导出整理一下，加个 `setup.py` console_scripts 入口让 `pip install` 后能 `mcc` 命令调用。

---

## 五、性能 & 可靠性

### Q25. 项目有哪些已知的性能瓶颈？

按影响排序：

1. **`asyncio.to_thread` 包装阻塞工具**：每个 sync 工具用一个线程池槽位。并行 10 个 bash 调用时，默认线程池（CPU × 5）可能被占满。可以让用户在 Config 里调 `max_workers`。
2. **Hook 超时 30s 串行执行**：多个 hook 配同一事件时按顺序跑。如果都很慢，turn 整体延迟翻倍。可以并行跑（独立 hook 互不依赖），但要小心副作用顺序——折中是给 hook 加 priority 字段。
3. **OpenAI 兼容的 message 翻译每次重做**：context 里 100 条消息时，每个 turn 都从头翻一遍。可以缓存"已翻译的前缀"，但要小心 cache 失效（context 压缩、resume）。当前规模下不值得。
4. **Telemetry 累计 cumulative 是 O(n)**：每次 `cumulative` property 都遍历整个 turns 列表。1000 个 turn 时仍然是几毫秒，可以接受；上万就要存增量和。

---

### Q26. 进程被 kill 之后会丢什么？

按"丢的严重程度"排序：

| 数据 | 是否丢 | 备注 |
|---|---|---|
| 当前正在跑的 turn 的 tool 输出 | 丢 | 没机制做"in-flight checkpoint" |
| 正在写的 session 文件 | **不丢**（原子写） | 见 [§10](technical-details.md#10-session-原子写) |
| 上一个 turn 完成时已写的 session | **不丢** | |
| Telemetry 历史 | **不丢** | session 里有 cumulative，但 per-turn 历史没存 |
| TodoStore | **不丢** | session 里完整 |
| Skills / Slash commands / Settings | **不丢** | 都是文件，重启会重新加载 |

最大的丢：当前正在跑的 turn 中途崩，工具已经执行了的副作用（创建的文件、跑过的命令）保留在文件系统，但 LLM 的 partial 响应丢了，conversation context 停在 user message 上。

恢复方式：`--resume <id>` 加载到上次完整结束的 turn，用户重新发一遍 prompt。

---

### Q27. 怎么定位 agent 跑得慢？哪些日志可以看？

按粒度从高到低：

1. **`/usage` 命令**：显示 per-turn / cumulative token + USD。如果某个 turn 异常高，是模型在长生成
2. **每个 tool dispatch 的状态行**：`-> [OK] file1\nfile2\n...`，时间戳没有但顺序看得出
3. **Hook 执行日志**：`[hook ok] cmd_xx` / `[hook block] ...`，能看出哪些 hook 卡了
4. **stderr 上的 compaction 提示**：`[compaction skipped: ...]` 表示 Haiku 调用失败
5. **session.json**：完整 conversation history，事后用 jq 分析

弱项：没有按 phase 的 timing breakdown。如果加，最容易的是给 agent_loop 关键节点加 `time.perf_counter()`，存到 telemetry 里。这是上面 Q25 提到的 hook timing 改进的一部分。

---

## 六、协作 & 项目管理

### Q28. 项目分 5 个 phase 的好处是什么？

**好处**：

- 每个 phase 都有明确的 done 条件（"所有测试过 + demo 跑通"）。增量交付，不会陷入 P1→P5 全做完才能验证的死循环
- 测试随实现走。P1 实现的功能 P5 时还能保证不被破坏（131 个测试覆盖累积）
- 遇到设计问题可以**回看 plan.md**，避免后期争论"原本是怎么想的"
- 进度可见。任何时刻都能说"P3 done，P4 在做 hooks"

**坏处**：

- Phase 边界有时候被我自己破坏（比如 P1 就引入了 LLMClient 抽象——技术上是 P5 才需要的）。这种"提前抽象"是有意识的，但确实让 P1 比纯净版多 ~150 行
- 严格按 phase 走可能错过更好的全局最优解。比如 P3 加 subagent 时如果一并把"subagent 共享 telemetry"想到了，P4 就不用回头改 [task_tool.py](../codelet/tools/task_tool.py) 了

总体看，分阶段推进的复利收益远大于成本。

---

### Q29. 如果团队来接手，最该读什么？

读顺序：

1. [README.md](../README.md) — 5 分钟看懂项目能干什么
2. [docs/architecture.md](architecture.md) — 模块图 + 责任划分
3. [docs/flow-diagrams.md](flow-diagrams.md) — 一个 turn 怎么跑、subagent 怎么 fork
4. [codelet/agent_loop.py](../codelet/agent_loop.py) 全文 — 核心循环就这一个文件，读完心里有数
5. [docs/technical-details.md](technical-details.md) — 哪些地方有坑，挑感兴趣的章节
6. 想加新 provider 看 [llm/openai_compat.py](../codelet/llm/openai_compat.py) 抄一份

测试是文档：每个测试文件 docstring 都说明了"在防御什么 invariant"。读测试比读源码更快理解关键约束。

---

### Q30. 最关键的"如果我离职新人需要知道的事"是什么？

三件事：

1. **Anthropic API 要求 tool_use / tool_result 顺序严格一致**。不要乱动 [`_dispatch_parallel`](../codelet/agent_loop.py)。如果将来真有原因要改，先看 [test_parallel_dispatch.py](../tests/test_parallel_dispatch.py)。

2. **subagent 的 task / todo_write 工具**必须重绑到 child loop。不要"为了节省内存复用父级实例"——会破坏深度上限和 todo 隔离。看 [`_wire_dynamic_tools`](../codelet/agent_loop.py)。

3. **OpenAI 兼容客户端的 6 个边界条件**全在 [test_openai_compat.py](../tests/test_openai_compat.py)。新加 provider 跑这套测试，全过基本就能用。

如果只能记一条，是第一条。
