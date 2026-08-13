# 文档索引

| 文档 | 适合谁读 | 内容 |
|---|---|---|
| [architecture.md](architecture.md) | 想看整体设计 | 系统组件图、模块职责、扩展点 |
| [flow-diagrams.md](flow-diagrams.md) | 想理解运行时行为 | 9 个 mermaid 时序图：启动、单工具、并行、subagent、hooks、压缩、resume、OpenAI 翻译、sync↔async 桥接 |
| [technical-details.md](technical-details.md) | 想知道实现细节为什么这么写 | 12 个深入主题：顺序保持、上下文隔离、深度上限、动态工具重绑、压缩安全、hooks 协议、配置优先级、OpenAI 边界、原子写、sync/async 桥接、diff 注入点 |
| [interview-qa.md](interview-qa.md) | 想看设计权衡 / 准备讲项目 | 30 个 Q&A：架构决策、实现陷阱、未做的事、性能、可靠性、协作 |
| [implementation-plan.md](implementation-plan.md) | 想看历史 | P1 → P5 的原始实现计划 |

## 推荐阅读顺序

**新接手项目** → architecture → flow-diagrams → 主代码（[agent_loop.py](../codelet/agent_loop.py)）→ technical-details 挑感兴趣的章节

**面试 / 讲解项目** → interview-qa → 配合 architecture / technical-details 找细节

**写新功能** → architecture 找扩展点 → 对应 phase 在 implementation-plan 里有什么承诺 → 模仿同类型现有实现

**调 bug** → flow-diagrams 看相关流程 → technical-details 找已知的 invariant → 测试文件的 docstring 说明了"在防御什么"
