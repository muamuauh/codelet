# 技术细节深入

本文档把每个有"坑"的实现点单独拎出来讲透。读完之后你应该能解释**为什么**代码长成现在这样，以及如果改一个看似小的地方会触发什么连锁反应。

如果只是想理解架构，先读 [architecture.md](architecture.md)。如果想看面试式 Q&A，去 [interview-qa.md](interview-qa.md)。

---

## 目录

1. [并行 dispatch 的顺序保持](#1-并行-dispatch-的顺序保持)
2. [SubAgent 上下文隔离](#2-subagent-上下文隔离)
3. [SubAgent 深度上限的实现](#3-subagent-深度上限的实现)
4. [动态工具的重绑定](#4-动态工具的重绑定task--todo_write--skill)
5. [Skill 索引的容量控制](#5-skill-索引的容量控制)
6. [Context 压缩的安全切片](#6-context-压缩的安全切片)
7. [Hooks 协议设计](#7-hooks-协议设计)
8. [配置解析优先级](#8-配置解析优先级)
9. [OpenAI 兼容层的边界条件](#9-openai-兼容层的边界条件)
10. [Session 原子写](#10-session-原子写)
11. [Sync ↔ Async 工具桥接](#11-sync-async-工具桥接)
12. [Diff 预览的注入点选择](#12-diff-预览的注入点选择)

---

## 1. 并行 dispatch 的顺序保持

**问题**：Anthropic API 强制要求 `tool_result` 数组的顺序与 `tool_use` 数组完全一致。一旦错位，API 返回 400，错误信息晦涩，调试地狱。

**实现** ([agent_loop.py](../codelet/agent_loop.py) `_dispatch_parallel`)：

```python
async def _dispatch_parallel(self, tool_calls: list[ToolCall]):
    if len(tool_calls) == 1:
        return [await self._dispatch_one(tool_calls[0])]   # 单调用快路径

    coros = [self._dispatch_one(call) for call in tool_calls]
    completed = await asyncio.gather(*coros, return_exceptions=True)

    by_id: dict[str, dict] = {}
    for call, item in zip(tool_calls, completed):
        if isinstance(item, BaseException):
            by_id[call.id] = self._error_result(call.id, f"Dispatcher error: {item}")
        else:
            by_id[call.id] = item

    # ⭐ 关键：按 tool_calls 原顺序遍历，不是按 completed
    return [by_id[call.id] for call in tool_calls]
```

**为什么不直接 `return list(completed)`**：`asyncio.gather` 实际上**会按入参顺序返回**结果（这是 contract），但显式按 `tool_use_id` 重组有两个好处：

1. 防御未来的 contract 变化或自定义调度器破坏顺序
2. 让"顺序保持"这个约束在代码里**可见**，新人改代码时不会无意中破坏

**测试** ([test_parallel_dispatch.py](../tests/test_parallel_dispatch.py)) 故意用三个不同 sleep 时长（300/100/200ms）的工具，验证完成顺序与 emit 顺序错开时仍按原序回填。

---

## 2. SubAgent 上下文隔离

**问题**：subagent 必须看不到父级的对话历史。如果父级的 prompt 里有敏感信息，泄漏到 subagent 的 LLM 调用就完蛋。

**实现** ([subagent/runner.py](../codelet/subagent/runner.py))：

```python
child = AgentLoop(
    config=child_config,
    registry=registry,                # 复制版（见 §4）
    client=self._client,              # 共享
    skill_index=self._skill_index,    # 共享
    hook_runner=self._hook_runner,    # 共享
    telemetry=self._telemetry,        # 共享
    allowed_tools=spec.allowed_tools,
    _is_subagent=True,
)
child.context = ConversationContext(config=child_config)   # ⭐ 全新空 context
child.context.depth = self._parent_depth + 1
child.context.set_system_prompt(_build_subagent_prompt(...))  # SubAgent 模板，不含父信息

summary_text = await child.run_async(spec.prompt)  # 只把 spec.prompt 喂进去
```

**测试中如何验证零泄漏** ([test_subagent.py](../tests/test_subagent.py))：父级用一个独特 marker `SECRET_PARENT_MARKER` 作为 user message，run 完之后遍历每个 subagent 的 chat 调用，断言这个 marker 从未出现在 subagent 的 messages 字段中。

**早期踩过的坑**：测试一开始用 `"SubAgent" in system` 来判断当前 chat 是不是 subagent。结果父级的 system prompt 里因为列出了所有工具描述，TaskTool.description 里有 `"Spawn a SubAgent to..."` —— 这个词 **也** 出现在父级 system 里，导致测试把父级的 chat 误判成 subagent，"误以为有泄漏"。修法：换更精确的 marker `"You are a SubAgent"`（只出现在 SubAgent 的系统模板首行）。

---

## 3. SubAgent 深度上限的实现

**问题**：递归 + 每个 subagent 自带 context = token 指数增长。必须有硬上限。

**关键决策**：上限 = 2，意思是：
- root agent (depth=0) → child (depth=1) ✓
- child (depth=1) → grandchild (depth=2) ✓
- grandchild (depth=2) → great-grandchild (depth=3) ✗ 拒绝

**实现** ([subagent/runner.py](../codelet/subagent/runner.py))：

```python
MAX_SUBAGENT_DEPTH = 2

@property
def at_depth_cap(self) -> bool:
    return self._parent_depth + 1 > MAX_SUBAGENT_DEPTH

async def run(self, spec: SubAgentSpec) -> SubAgentResult:
    if self.at_depth_cap:
        return SubAgentResult(
            summary=f"SubAgent rejected: depth cap reached ...",
            turns=0,
            depth_capped=True,
        )
    # ... 正常 fork 逻辑
```

**关键安全点**：拒绝时**零 LLM 调用**。测试 (`test_depth_cap_refuses_when_parent_is_already_at_max`) 拿个 ScriptedClient 跑 spawn，断言 `client.history == []`。这意味着即使 LLM 疯狂请求 `task`，也不会烧钱。

**配套**：每个 subagent 还有 turn 上限 = 8 (`PER_SUBAGENT_TURN_CAP`)，防止单个 subagent 自己卡循环。

---

## 4. 动态工具的重绑定（task / todo_write / skill）

**问题**：subagent 复用父级的 ToolRegistry 时，`task` 和 `todo_write` 工具实例**不能直接共享**。

```python
class TaskTool(Tool):
    def __init__(self, parent: AgentLoop):
        self._parent = parent          # ⚠️ 闭包到父级
    async def aexecute(self, params):
        ...
        parent_depth=self._parent.context.depth   # 读父级深度！
```

如果 subagent 直接用父级的 `TaskTool` 实例，subagent 想再 spawn 时读到的是**父级的 depth=0** 而不是自己的 depth=1，深度上限失效，递归无限。

**实现** ([agent_loop.py](../codelet/agent_loop.py) `_wire_dynamic_tools`)：

```python
def _wire_dynamic_tools(self, allowed_tools):
    allow = set(allowed_tools) if allowed_tools is not None else None

    def maybe(name: str, factory):
        if allow is not None and name not in allow:
            return
        self.registry.unregister(name)        # ⭐ 先剥掉继承来的实例
        self.registry.register(factory())     # ⭐ 再绑到 self

    maybe("task", lambda: TaskTool(self))
    if self.skill_index.names():
        maybe("skill", lambda: SkillTool(self.skill_index))
    maybe("todo_write", lambda: TodoWriteTool(self.todo_store))
```

每个 AgentLoop（包括 subagent）init 时都会**剥离**任何继承自父 registry 的 task/todo_write，**然后绑定到自己**。registry 用了 shallow copy（[runner.py](../codelet/subagent/runner.py) `_shallow_copy_registry`）所以这个 unregister 不会污染父级的 registry。

**Skill 工具的特殊点**：只在 `self.skill_index.names()` 非空时注册。这样不装 skill 的用户不会看到一个废工具占 tokens。

**`allowed_tools` 白名单**：subagent 可以在 spawn 时只暴露子集。AgentLoop init 在 wire 之前先把 registry 中**不在白名单**的工具 unregister，然后 `maybe()` 也跳过白名单外的 task/todo/skill。两层共同保证白名单严格。

---

## 5. Skill 索引的容量控制

**问题**：skill 越积越多时，system prompt 会被吃光。

**两层防御** ([skills/loader.py](../codelet/skills/loader.py))：

```python
@dataclass
class SkillIndex:
    skills: dict[str, Skill] = field(default_factory=dict)
    max_in_index: int = 30   # ⭐ 硬上限

    def index_summary(self) -> str:
        lines = []
        for skill in list(self.skills.values())[: self.max_in_index]:
            desc = skill.description.replace("\n", " ").strip()
            if len(desc) > 80:                       # ⭐ 单行 80 字符上限
                desc = desc[:77] + "..."
            lines.append(f"- {skill.name}: {desc}")
        if len(self.skills) > self.max_in_index:
            lines.append(f"- (... {len(self.skills) - self.max_in_index} more skills truncated)")
        return "\n".join(lines)
```

System prompt 中只放 `name: description` 单行，**完整 body 在 `skill` 工具背后**。模型决定哪个 skill 相关后通过 `skill(name=...)` 工具调用按需取出。

这个设计的副作用：用户没法**强制**把一个 skill 的 body 塞进 system prompt — 模型必须主动取。但这正是我们想要的：把"何时需要这个知识"的判断交给模型，避免每次都加载所有 procedure。

---

## 6. Context 压缩的安全切片

**问题**：Anthropic API 要求每个 `tool_use` block 后必须紧跟其 `tool_result`。**如果压缩切片切到这种配对的中间，下一次 API 调用直接 400**。

**实现** ([context.py](../codelet/context.py))：

```python
async def compact_if_needed(self, client: LLMClient) -> bool:
    if not self.should_compact(client):
        return False

    keep_recent = max(2, self.config.compact_keep_recent)
    if len(self.messages) <= keep_recent + 1:
        return False  # 没有足够的"中段"可压

    head = self.messages[:1]                      # 第一条（种子任务）保留
    middle = self.messages[1:-keep_recent]        # 待总结
    tail = self.messages[-keep_recent:]           # 末尾 keep_recent 条保留

    if not middle:
        return False

    summary_text = await _summarize(client, middle, self.config)
    summary_message = {
        "role": "user",
        "content": f"<conversation_summary>\n{summary_text}\n</conversation_summary>",
    }
    self.messages = head + [summary_message] + tail
    self.compactions += 1
    return True
```

**为什么 keep_recent ≥ 2**：默认 4。任何"工具调用 + 结果"配对最多占 2 条消息（assistant tool_use + user tool_result），keep 4 给一个 buffer：哪怕最近两轮各有一对配对也能完整保留。

**触发时机**：在 [agent_loop.py](../codelet/agent_loop.py) `run_async` 里，**只在一个 turn 完整结束后**调用：

```python
self.context.add_assistant_message(response.raw_content)
tool_results = await self._dispatch_parallel(...)
self.context.add_tool_results(tool_results)

# ⭐ 此刻 tool_use 和 tool_result 一定已经配对完整
try:
    await self.context.compact_if_needed(self.client)
except Exception as exc:
    self.console.print(f"[dim yellow][compaction skipped: {exc}][/dim yellow]")
```

**异常吞掉**：summarizer 调用 Haiku 可能因为各种原因失败（API 限流、网络抖动、key 失效）。**主 agent 不能因为压缩失败崩**。所以包了 try/except，失败仅打 warning。

**测试中如何验证单调性** ([test_compaction.py](../tests/test_compaction.py))：跑 5 次 `compact_if_needed`，断言 `sizes == sorted(sizes, reverse=True)` —— 压缩永远不会增长 message count，否则就是死循环 bug。

---

## 7. Hooks 协议设计

**协议** ([hooks/runner.py](../codelet/hooks/runner.py))：

| 事件 | 触发时机 | 阻断方式 | 可重写字段 |
|---|---|---|---|
| `UserPromptSubmit` | REPL 收到用户消息后 | exit ≠ 0 → `PromptBlocked` exception | stdout JSON `{"prompt": "..."}` |
| `PreToolUse` | tool 执行前 | exit ≠ 0 → 转 `is_error` tool_result | stdout JSON `{"tool_input": {...}}` |
| `PostToolUse` | tool 执行后 | **永不阻断**（best-effort 日志） | — |

**通信**：每个 hook 是一条 shell 命令。stdin 收到 JSON 事件，stdout 可输出 JSON 重写值。

```python
proc = subprocess.run(
    command,
    shell=True,                       # 跨平台：cmd.exe / sh / 任何用户安装的 shell
    input=json.dumps(payload),
    capture_output=True,
    text=True,
    timeout=HOOK_TIMEOUT_SECONDS,     # = 30
)
```

**为什么 `shell=True` 而不是 argv list**：用户可能写 `echo X | grep Y && exit 1` 这种带管道和 `&&` 的命令。让 shell 解释最自然。代价：用户必须自己处理引用 — 这是协议约定，文档化。

**为什么 PostToolUse 不能阻断**：因为 tool 已经跑完了。文件已经写了/进程已经启动了。让 PostToolUse 阻断只会让 tool_result 错乱。它的用途是日志、metric 上报、安全审计。

**Matcher 简化策略**：只支持 `*` / 精确名 / 逗号列表。**不支持正则**。原因：正则给用户太多绳子上吊，配置文件里的 regex 极易写错且难调试。需要复杂匹配的用户可以让 hook 命令自己读 stdin 后做判断。

---

## 8. 配置解析优先级

**层级**（高 → 低）：

```
1. CLI flags             (--provider, --model, --base-url, --api-key, --mode, ...)
2. CLI --profile X       (从 settings.profiles[X] 取四元组)
3. settings.json profile (settings["profile"] 字段指定的默认)
4. LLM_* env-driven      (LLM_BASE_URL/LLM_API_KEY/LLM_MODEL)
5. "anthropic" 默认
```

**实现位置**：[settings.py](../codelet/settings.py) `resolve_profile` + [cli.py](../codelet/cli.py) `_build_config`。

**为什么 LLM_* 路径放在第 4 层而不是第 3 层**：用户在 settings.json 里**显式**写了 `"profile": "X"`，那是更明确的意图，应该胜过 .env 里的隐式配置。但如果 settings.json 没设默认 profile，LLM_* 应该自动生效（这就是用户最常见的 quick-start 路径）。

**`--base-url` 不带 `--provider` 自动推断 OpenAI** ([cli.py](../codelet/cli.py))：

```python
if args.provider is not None:
    cfg.provider = LLMProvider(args.provider)
elif args.base_url is not None and args.profile is None:
    # 用户给了 --base-url 但没指定 provider 也没选 profile -- 几乎肯定是
    # OpenAI 兼容端点（Anthropic 用户极少改 base_url）
    cfg.provider = LLMProvider.OPENAI
```

边界：`--profile X` + `--base-url` 时**不**推断（profile 已经定了 provider），让 `--base-url` 单纯作为 override。

**Inline api_key 优先级**：profile 里如果同时写了 `api_key` 和 `api_key_env`，inline 胜出。这样 self-contained 配置（单个 settings.json 可分发）能正常工作，不会被偶然存在的同名 env var 污染。

---

## 9. OpenAI 兼容层的边界条件

[openai_compat.py](../codelet/llm/openai_compat.py) 处理了一堆"看似 OpenAI 兼容"的中转站会偷偷踩的雷：

### 9.1 仅 tool_calls 时 content 必须是 None

```python
msg_out = {
    "role": "assistant",
    "content": merged_text or None,   # ⭐ "" 会被 OpenAI SDK 拒绝
}
if tool_calls:
    msg_out["tool_calls"] = tool_calls
```

OpenAI SDK 在 assistant 消息**只**有 tool_calls 而没有文本时，要求 `content=None`。空字符串 `""` 会触发 schema 错误（部分中转站直接 500）。

### 9.2 空 tools 列表必须省略字段

```python
kwargs = {"model": model, "messages": oa_messages, "max_tokens": max_tokens}
if tools:
    kwargs["tools"] = self._to_openai_tools(tools)
# 不要写 kwargs["tools"] = []
```

部分中转（比如某些自建 vLLM proxy）对空 tools 数组的处理不一致：有的接受、有的报错说"必须省略"。最稳妥是空就不传。

### 9.3 tool_calls.function.arguments 必须 JSON 字符串

Anthropic 内部用 dict，OpenAI 用字符串。翻译时：

```python
{"function": {"name": ..., "arguments": json.dumps(input_dict or {})}}
```

回程更脏：部分中转（特别是小模型）偶尔回吐**畸形 JSON**（缺引号、单引号、JSON5 等）。我们用 try/except 降级到 `{}`，避免崩 agent loop：

```python
try:
    parsed = json.loads(args_raw) if args_raw else {}
except json.JSONDecodeError:
    parsed = {}
```

### 9.4 is_error 内联前缀

OpenAI 协议没有 tool_result 的 `is_error` 字段。我们把错误信息内联到 content 前缀：

```python
if tr.get("is_error"):
    body = f"[ERROR] {body}"
out.append({"role": "tool", "tool_call_id": tr["tool_use_id"], "content": str(body)})
```

模型读到 `[ERROR]` 前缀就知道工具失败了，从行为上和 Anthropic 一致。

### 9.5 finish_reason 映射

| OpenAI | Anthropic | 处理 |
|---|---|---|
| `tool_calls` | `tool_use` | 主循环判断条件统一 |
| `stop` | `end_turn` | 同 |
| `length` | `max_tokens` | 同 |
| 其他 | passthrough | 不映射 |

让上层 agent_loop 完全不关心 provider 是哪个。

### 9.6 tool schema 字段名

| Anthropic | OpenAI |
|---|---|
| `input_schema` | `function.parameters` |
| `description` | `function.description` |
| `name` | `function.name` |

加 `{"type": "function"}` 包装。Anthropic 没有这层包装。

---

## 10. Session 原子写

**问题**：session 文件可能在写入过程中进程被 kill -9。如果直接 `f.write(json)`，留下半 JSON，下次 `load_session` 直接报 JSONDecodeError，用户重启就丢上下文。

**实现** ([persistence/session.py](../codelet/persistence/session.py))：

```python
def _write_atomic(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_name, path)              # ⭐ 原子换名
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path
```

**关键技术**：`os.replace` 在大多数 POSIX 文件系统和 NTFS 上是**原子操作**。要么旧文件（被替换）要么新文件，永远不会出现"半新半旧"。

**临时文件清理**：在异常路径里 `os.unlink(tmp_name)`，否则崩了会留 `*.tmp` 垃圾。测试 (`test_atomic_write_no_partial_files`) 用 monkeypatch 让 `os.replace` 抛异常，然后断言 `tmp_path.glob("*.tmp") == []`。

**为什么不用 `f.flush(); os.fsync(); rename`**：那是更严格的"持久化保证"，需要对崩溃恢复语义有要求时才用。我们的需求是"重启不丢"，不是"断电不丢"。`os.replace` 已经够。

---

## 11. Sync ↔ Async 工具桥接

**问题**：大多数工具是阻塞的（subprocess、文件 IO）；少数是天然 async 的（httpx、subagent）。我们想让 agent loop 全异步，但又不想强迫每个工具作者写 async。

**方案** ([tools/base.py](../codelet/tools/base.py))：

```python
class Tool(ABC):
    def execute(self, params) -> ToolResult:
        """同步实现的接缝。阻塞型工具 override 这个。"""
        raise NotImplementedError(f"{type(self).__name__} has no sync execute()")

    async def aexecute(self, params) -> ToolResult:
        """agent loop 调的入口。默认把 execute 丢线程池。"""
        return await asyncio.to_thread(self.execute, params)
```

- **Sync 工具**（Bash/FileRead/Write/Edit/Glob/Grep）只实现 `execute`，自动获得 `aexecute` 的线程包装
- **Async 工具**（WebFetch/Task）直接 override `aexecute`，不浪费线程

这种设计是有意识的取舍：

> 把 `execute` 设为**非抽象**（默认 raise NotImplementedError）让 async-native 工具不必给 sync 路径写 stub。代价是 `Tool` 类不再保证有 `execute` —— 这是可接受的，注册器和 loop 都只调 `aexecute`，不会触发那个 `NotImplementedError`。

**LLM 调用同样套这个壳子**：[agent_loop.py](../codelet/agent_loop.py)：

```python
return await asyncio.to_thread(
    self.client.chat,                   # 同步 SDK
    messages=..., system=..., tools=..., model=..., max_tokens=...,
)
```

将来想换 `AsyncAnthropic` 只改这一行。

---

## 12. Diff 预览的注入点选择

**问题**：ASK 模式下，`write_file` / `edit_file` 真正动磁盘前，应该让用户看 diff 并 y/n 确认。注入点选哪里？

**候选位置**：

| 选项 | 优点 | 缺点 |
|---|---|---|
| 工具内部 `execute` 顶部 | 最自然 | 工具变得不纯，且需要持有 console 引用 |
| `PermissionGate.check` | 已有"判断能否执行"逻辑 | 同步 + 没有 console |
| `agent_loop._dispatch_one` | 有 console 和 config，集中处理 | 工具特定逻辑泄露到 loop |

**选择**：第三个。理由：
1. 已经在 dispatch 路径上，已有 console
2. 子类化测试容易：注入 `confirm_callback`
3. 工具只暴露 `preview_diff(params) -> str | None` 数据，不持有 UI 概念

**实现** ([agent_loop.py](../codelet/agent_loop.py))：

```python
_DIFF_CONFIRM_TOOLS = {"write_file", "edit_file"}

# 在 _dispatch_one 里，permission_gate 之后、aexecute 之前：
if (
    self.config.permission_mode == PermissionMode.ASK
    and call.name in _DIFF_CONFIRM_TOOLS
    and self._confirm_callback is not None
):
    preview = tool.preview_diff(tool_input)
    if preview:
        if not self._confirm_callback(call.name, preview):
            return self._error_result(call.id, "User rejected the proposed change.")
```

**Subagent 永远跳过确认**：`_is_subagent=True` 时 `_confirm_callback=None`。理由：subagent 跑在后台，用户看不到 prompt，弹确认会**永远卡住**。

**测试技巧** ([test_diff_preview.py](../tests/test_diff_preview.py))：传入一个 `confirm_callback=lambda *_: False` 的 lambda 模拟用户拒绝，断言文件没被改。
