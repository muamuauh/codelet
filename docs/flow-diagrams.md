# 运行时流程图

本文档用 mermaid 时序图展示几个关键运行时路径。配合 [architecture.md](architecture.md) 看模块责任，配合 [technical-details.md](technical-details.md) 看实现细节。

GitHub / VSCode / Typora 都能直接渲染 mermaid。

---

## 1. 启动到第一个 turn 的完整路径

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant CLI as cli.main
    participant ENV as load_env_files
    participant SET as load_settings
    participant RES as resolve_profile
    participant CFG as Config
    participant FAC as build_client
    participant AGT as AgentLoop.__init__
    participant SK as load_skills
    participant SC as load_commands
    participant SS as SessionStore

    U->>CLI: python -m codelet
    CLI->>ENV: 读 .env / ~/.codelet/.env
    ENV-->>CLI: os.environ 已注入
    CLI->>SET: 合并 user + project settings.json
    SET-->>CLI: settings dict
    CLI->>RES: resolve_profile(settings, args.profile)
    Note over RES: 1.CLI --profile<br/>2.settings.profile<br/>3.LLM_* env-driven<br/>4."anthropic"
    RES-->>CLI: (provider, base_url, model, api_key)
    CLI->>CFG: 组装 Config (CLI flags 最高优)
    CLI->>FAC: build_client(config)
    FAC-->>CLI: AnthropicClient | OpenAICompatClient
    CLI->>AGT: AgentLoop(config, registry, client, ...)
    AGT->>SK: load_skills() (project + user)
    AGT->>AGT: _wire_dynamic_tools() 绑 task/skill/todo_write 到 self
    AGT->>AGT: build_system_prompt (含 skill 索引)
    CLI->>SC: load_commands() (project + user)
    CLI->>SS: SessionStore() 准备 auto-save
    CLI-->>U: REPL 提示符 ">"
```

---

## 2. 一个 user turn：单工具串行

最简单的情况 — LLM 想用 1 个工具。

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant LP as AgentLoop.run_async
    participant H as HookRunner
    participant CTX as Context
    participant LLM as LLMClient.chat
    participant DSP as _dispatch_one
    participant T as Tool
    participant TEL as Telemetry

    U->>LP: "在当前目录找出所有 TODO"
    LP->>TEL: begin_user_turn() (mark boundary)
    LP->>H: fire("UserPromptSubmit", {prompt})
    Note right of H: 可改写 prompt 或<br/>exit≠0 → PromptBlocked
    H-->>LP: outcome.overrides / blocked
    LP->>CTX: add_user_message(prompt)

    loop 直到无 tool_use 或 max_turns
        LP->>LLM: chat(messages, system, tools)
        LLM-->>LP: LLMResponse(text + tool_calls + usage)
        LP->>TEL: record_chat(model, usage)

        alt response 仅含 text (end_turn)
            LP->>CTX: add_assistant_message(raw_content)
            Note over LP: 跳出循环
        else response 含 tool_use
            LP->>CTX: add_assistant_message(raw_content)
            LP->>DSP: dispatch (单调用快路径)
            DSP->>H: fire("PreToolUse", {tool_name, tool_input})
            H-->>DSP: 可改写 tool_input 或阻断
            DSP->>DSP: PermissionGate.check
            opt ASK 模式 + write/edit 工具
                DSP->>DSP: tool.preview_diff()
                DSP->>U: 显示彩色 diff + y/N 提示
                U-->>DSP: y or n
            end
            DSP->>T: await tool.aexecute(tool_input)
            T-->>DSP: ToolResult(output, is_error)
            DSP->>H: fire("PostToolUse", ...) (best-effort)
            DSP-->>LP: tool_result block
            LP->>CTX: add_tool_results([block])
            LP->>CTX: compact_if_needed (token threshold)
        end
    end

    LP-->>U: final assistant text
    Note over LP: REPL 渲染 telemetry panel<br/>SessionStore.record(agent)
```

> 注意第 4 步的 `begin_user_turn()`：telemetry 拿这个 marker 拆"本轮 token vs 累计 token"。

---

## 3. 并行 dispatch + 顺序保持

LLM 一个 turn 里发出 N 个 tool_use 时的关键路径。**这是整个项目最容易踩坑的点**：Anthropic API 强制要求 `tool_result` 顺序与 `tool_use` 完全一致，错位会 400。

```mermaid
sequenceDiagram
    autonumber
    participant LP as run_async
    participant DSP as _dispatch_parallel
    participant T1 as Tool A (300ms)
    participant T2 as Tool B (100ms)
    participant T3 as Tool C (200ms)
    participant CTX as Context

    Note over LP: response.tool_calls = [A, B, C]<br/>原始 emit 顺序
    LP->>DSP: dispatch_parallel([A, B, C])

    par 并发执行
        DSP->>T1: aexecute (300ms)
    and
        DSP->>T2: aexecute (100ms)
    and
        DSP->>T3: aexecute (200ms)
    end

    Note over T2: 100ms 后先完成
    T2-->>DSP: result_B (id=t-B)
    Note over T3: 200ms 完成
    T3-->>DSP: result_C (id=t-C)
    Note over T1: 300ms 最后
    T1-->>DSP: result_A (id=t-A)

    Note over DSP: by_id = {t-A: ..., t-B: ..., t-C: ...}<br/>按 tool_calls 原顺序遍历
    DSP-->>LP: [result_A, result_B, result_C]
    LP->>CTX: add_tool_results 严格按原顺序
```

**任一工具抛异常**：被 `asyncio.gather(return_exceptions=True)` 捕获，转成 `is_error=true` 的 tool_result，兄弟工具继续完成。

---

## 4. SubAgent 派发（含并行）

模型在一个 turn 里同时发两个 `task` 时：

```mermaid
sequenceDiagram
    autonumber
    participant LP0 as 父 AgentLoop (depth=0)
    participant DSP0 as 父 _dispatch_parallel
    participant TT as TaskTool
    participant SS as SubAgentSession
    participant LP1A as 子 AgentLoop A (depth=1)
    participant LP1B as 子 AgentLoop B (depth=1)
    participant LLM as LLMClient

    LP0->>DSP0: dispatch_parallel([task-A, task-B])
    par 并行派发两个 task
        DSP0->>TT: aexecute({prompt: "do A"})
        TT->>SS: SubAgentSession(parent_depth=0)
        SS->>SS: depth check (0+1=1 ≤ 2) ✓
        SS->>LP1A: 新 AgentLoop(_is_subagent=True, depth=1)
        Note over LP1A: 共享 client/registry/skill_index/<br/>hooks/telemetry<br/>但 context 是全新的
        LP1A->>LP1A: 重绑 task/todo_write 到自己<br/>(parent 的会指向错的 depth)
        LP1A->>LP1A: 用 SubAgent system prompt
        LP1A->>LLM: chat (subagent 自己的 messages)
        LP1A->>LP1A: 自己的工具循环
        LP1A-->>SS: 最终 assistant text (≤4KB summary)
        SS-->>TT: SubAgentResult(summary, turns, tools_used)
        TT-->>DSP0: ToolResult(summary + metadata)
    and
        DSP0->>TT: aexecute({prompt: "do B"})
        TT->>SS: 同上
        SS->>LP1B: 同上
        LP1B-->>SS: 自己的 summary
        SS-->>TT: ...
        TT-->>DSP0: ToolResult B
    end
    DSP0-->>LP0: [result_A, result_B] 按原 tool_use 顺序
```

**深度上限**：subagent (depth=1) 还能再 spawn (depth=2)，但 depth=2 spawn depth=3 会被拒绝（`session.run` 直接返回，**零 LLM 调用**）。详见 [interview-qa.md](interview-qa.md#为什么深度上限定为-2)。

---

## 5. Hooks 三事件全景

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant LP as run_async
    participant DSP as _dispatch_one
    participant T as Tool
    participant H as HookRunner
    participant SH as Shell

    U->>LP: prompt
    LP->>H: fire("UserPromptSubmit", {prompt})
    H->>SH: subprocess (shell=True, stdin=JSON, 30s timeout)
    SH-->>H: stdout / stderr / exit_code
    alt exit_code != 0
        H-->>LP: blocked + reason
        LP-->>U: PromptBlocked exception
    else stdout 是 JSON 含 prompt
        H-->>LP: overrides={"prompt": "改写后"}
        LP->>LP: user_message = overrides["prompt"]
    end

    LP->>DSP: tool 调用
    DSP->>H: fire("PreToolUse", {tool_name, tool_input})
    H->>SH: subprocess
    SH-->>H: ...
    alt blocked
        DSP-->>LP: is_error tool_result
    else stdout JSON 含 tool_input
        DSP->>DSP: tool_input = overrides["tool_input"]
    end

    DSP->>T: aexecute(possibly_overridden_input)
    T-->>DSP: result

    DSP->>H: fire("PostToolUse", {tool_name, tool_input, tool_output, is_error})
    Note over H: 永不阻断<br/>exit_code 仅记录
    H-->>DSP: outcome (ignored for blocking)
    DSP-->>LP: result_block
```

---

## 6. Context 自动压缩

每个 turn 收尾时（**绝不在 tool_use → tool_result 配对之间**）触发：

```mermaid
sequenceDiagram
    autonumber
    participant LP as run_async
    participant CTX as Context
    participant LLM as LLMClient (Haiku)

    Note over LP: 一个 turn 完整结束<br/>已 add_tool_results
    LP->>CTX: compact_if_needed(client)
    CTX->>CTX: estimate_tokens()
    CTX->>CTX: > context_window * compact_threshold_ratio?
    alt 不超 → 直接返回 False
        CTX-->>LP: False
    else 超阈值
        Note over CTX: head = messages[:1] (种子任务)<br/>tail = messages[-keep_recent:]<br/>middle = messages[1:-keep_recent]
        CTX->>LLM: chat(model=compact_model="claude-haiku-4-5",<br/>system="你是上下文总结助手",<br/>user=渲染中段)
        LLM-->>CTX: summary text
        CTX->>CTX: messages = head + [user("&lt;summary&gt;...")] + tail
        CTX->>CTX: compactions += 1
        CTX-->>LP: True
    end
    Note over LP: 失败则吞掉异常 + warning<br/>主 agent 不会因压缩死
```

**为什么不在 tool 配对中间压缩**：Anthropic API 要求每个 `tool_use` 后必须紧跟其 `tool_result`，如果切片切到中间会让 API 直接 400。

---

## 7. Session 持久化与恢复

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant CLI as cli.main
    participant SS as SessionStore
    participant FS as Filesystem
    participant AGT as AgentLoop

    Note over CLI: 启动时
    CLI->>SS: SessionStore() 生成新 id

    loop 每个 turn 后
        AGT-->>CLI: run_async 返回
        CLI->>SS: record(agent)
        SS->>SS: _serialize(agent, id, created_at)
        SS->>FS: tempfile.mkstemp(.tmp)
        SS->>FS: write(json) to tmp
        SS->>FS: os.replace(tmp -> final)
        Note over FS: 原子换名<br/>kill -9 也不会留半 JSON
    end

    Note over U: 下次启动
    U->>CLI: --resume 20260503-153045-abcd
    CLI->>FS: load_session(id)
    FS-->>CLI: snapshot dict
    CLI->>AGT: restore_into(agent, snapshot)
    Note over AGT: messages / system_prompt / depth /<br/>compactions / todos 都恢复<br/>工具/hooks/skills 来自当前 settings<br/>(改了 settings 重启会生效)
```

---

## 8. OpenAI 兼容客户端的双向翻译

非时序图，但展示一个 turn 的双向数据流。

```mermaid
flowchart LR
    subgraph "codelet 内部 (Anthropic-shaped)"
        IN_MSG["messages: list[dict]<br/>{role, content: str | [blocks]}<br/>blocks: text / tool_use / tool_result"]
        IN_TOOLS["tools: [{name, description, input_schema}]"]
        IN_SYS["system: str"]
    end

    subgraph "OpenAI Chat Completions API"
        OA_MSG["messages: list<br/>{role: system|user|assistant|tool,<br/>content: str|None,<br/>tool_calls?, tool_call_id?}"]
        OA_TOOLS["tools: [{type:function,<br/>function:{name, parameters}}]"]
    end

    IN_MSG -->|to_openai_messages| OA_MSG
    IN_TOOLS -->|to_openai_tools| OA_TOOLS
    IN_SYS -->|prepend as role:system| OA_MSG

    subgraph "Provider"
        DS["DeepSeek / OpenRouter /<br/>SiliconFlow / Ollama / ..."]
    end

    OA_MSG --> DS
    OA_TOOLS --> DS

    DS --> OA_RESP["choices[0].message<br/>{content?, tool_calls?,<br/>finish_reason}"]
    OA_RESP -->|_to_internal_response| RESP["LLMResponse<br/>{text_blocks, tool_calls,<br/>raw_content (Anthropic-shaped),<br/>stop_reason, usage}"]
```

关键翻译规则（详见 [technical-details.md](technical-details.md#openai-兼容层的边界条件)）：
- `tool_use` → OpenAI `tool_calls`，arguments 必须 JSON 字符串
- 仅 tool_calls 时 assistant.content 必须 `None` 而非 `""`
- `tool_result` → 单独一条 `role: "tool"` 消息，`is_error=True` 内联前缀 `[ERROR]`
- 空 tools 列表必须**省略**字段（不能传 `[]`，部分中转会拒）
- finish_reason `"tool_calls"` ↔ Anthropic `"tool_use"`，`"stop"` ↔ `"end_turn"`，`"length"` ↔ `"max_tokens"`

---

## 9. 工具的 sync ↔ async 桥接

为什么 `Tool.execute` 是 sync 但 loop 是 async？

```mermaid
flowchart TB
    LOOP["agent_loop._dispatch_one<br/>(async)"]

    subgraph SYNC_TOOLS["阻塞型工具 (大部分)"]
        BASH[BashTool.execute]
        FR[FileReadTool.execute]
        FW[FileWriteTool.execute]
    end

    subgraph ASYNC_TOOLS["原生 async 工具"]
        WF[WebFetchTool.aexecute]
        TT[TaskTool.aexecute]
    end

    AEX["Tool.aexecute (默认实现)<br/>= asyncio.to_thread(self.execute)"]

    LOOP -->|await tool.aexecute| AEX
    AEX --> SYNC_TOOLS
    LOOP -->|await tool.aexecute<br/>原生 override| ASYNC_TOOLS
```

**默认 aexecute** 把 sync `execute` 丢到 `asyncio.to_thread` 去跑（用线程池避免阻塞 event loop）。**原生 async 工具** 直接 override `aexecute`，不浪费线程。
