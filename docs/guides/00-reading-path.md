# DeerFlow 工程师阅读路径

> 面向想学习 Agent 架构、Harness 设计、系统架构的工程师，把 90+ 篇文档组织成一条从全景到精通的学习路径。

---

## 文档体系地图

```
docs/
├── guides/              ← 你在这里（学习路径 + 设计指南）
│   ├── 00-reading-path.md          本文档
│   ├── 01-architecture-decisions.md  架构决策记录（为什么这么做）
│   ├── 02-extension-guide.md        扩展指南（我想加一个 X）
│   └── 03-replaceability-map.md     可替换性地图（什么能换、什么不能）
│
├── core/                模块纵深文档（每个模块 4 层结构）
│   ├── agent/           Agent 系统：overview → lifecycle → capabilities → middlewares
│   ├── config/          配置系统：overview → app/model/infra/feature → lifecycle
│   ├── models/          模型工厂：overview → factory → providers → lifecycle
│   ├── memory/          记忆系统：system + design-decisions + implementation
│   ├── skills/          技能系统：overview → lifecycle → capabilities → policies
│   ├── subagents/       子代理：overview → registry → executor → builtins → lifecycle
│   ├── sandbox/         沙箱：overview → interface → tools → security → lifecycle
│   ├── tools/           工具系统：overview → builtins → sync → lifecycle
│   ├── mcp/             MCP：overview → cache → client → oauth → tools → lifecycle
│   ├── community/       社区工具：overview → search → web → aio-sandbox → lifecycle
│   ├── gateway/         Gateway：overview → api → auth → middleware → lifecycle
│   ├── uploads/         文件上传：overview
│   ├── persistence/     持久化：overview → run → event → thread → feedback → user → lifecycle
│   ├── channels/        IM 频道：overview → lifecycle → capabilities → policies
│   ├── guardrails/      安全护栏：overview → lifecycle → capabilities → policies
│   ├── reflection/      反射：overview
│   ├── tracing/         追踪：overview
│   ├── utils/           工具类：overview
│   └── runtime/         运行时：overview → lifecycle → capabilities → concurrency → streaming → tracking → instances → consistency
│
└── lifecycle/           跨模块链路文档（端到端串联）
    ├── 01-agent-request-flow.md     请求全流程
    ├── 02-context-compression.md    上下文压缩
    ├── 03-memory-context-chain.md   记忆上下文链路
    ├── 04-skills-loading-chain.md   技能加载链路
    ├── 05-subagent-dispatch.md      子代理派发
    └── 06-file-upload.md            文件上传
```

---

## 第一层：建立全景（~2 小时）

**目标**: 理解一次用户请求如何在系统中流转，建立整体心智模型。

**前置要求**: 无

| 顺序 | 文档 | 为什么读这篇 | 预计时间 |
|------|------|-------------|---------|
| 1 | [CLAUDE.md](../../CLAUDE.md) | 项目架构总览，所有模块的定位和职责 | 30 min |
| 2 | [lifecycle/01-agent-request-flow.md](../lifecycle/01-agent-request-flow.md) | 端到端追踪一次请求，理解所有组件的协作关系 | 30 min |
| 3 | [core/agent/01-overview.md](../core/agent/01-overview.md) | Agent 系统核心概念：中间件链、ReAct 循环、工具绑定 | 30 min |
| 4 | [core/config/00-overview.md](../core/config/00-overview.md) | 配置如何驱动整个系统，理解 config.yaml 的角色 | 15 min |

**学完后你能回答**:
- 一个用户消息从 HTTP 请求到 AI 回复经过了哪些步骤？
- 20 个中间件分别在什么时机执行？
- config.yaml 中的一行配置如何变成运行时的一个组件？

---

## 第二层：理解核心抽象（~3 小时）

**目标**: 掌握系统中最核心的 4 个设计模式，这些是理解所有后续模块的基础。

**前置要求**: 第一层

| 顺序 | 文档 | 核心概念 | 预计时间 |
|------|------|---------|---------|
| 5 | [guides/01-architecture-decisions.md](01-architecture-decisions.md) | 10 个关键设计决策的"为什么" | 40 min |
| 6 | [core/agent/05-middlewares.md](../core/agent/05-middlewares.md) | 中间件链的完整生命周期钩子详解 | 30 min |
| 7 | [core/models/00-overview.md](../core/models/00-overview.md) + [01-factory.md](../core/models/01-factory.md) | 反射模式：config.yaml → 类名 → 实例化 | 25 min |
| 8 | [core/sandbox/00-overview.md](../core/sandbox/00-overview.md) + [01-interface.md](../core/sandbox/01-interface.md) | 虚拟路径抽象：Local 和 AIO 的统一接口 | 20 min |
| 9 | [core/runtime/01-overview.md](../core/runtime/01-overview.md) + [02-run-lifecycle.md](../core/runtime/02-run-lifecycle.md) | 运行时生命周期：RunManager → Worker → Agent | 25 min |

**学完后你能回答**:
- 为什么用中间件链而不是继承？
- 为什么系统提示词必须保持静态？
- 虚拟路径系统解决了什么问题？
- 运行时如何管理并发请求？

---

## 第三层：按兴趣深入（每方向 ~2 小时）

前置要求: 第二层。以下方向可按兴趣选择，无顺序要求。

### 方向 A：工具与 MCP

| 文档 | 主题 |
|------|------|
| [core/tools/00-overview.md](../core/tools/00-overview.md) → [01-builtins.md](../core/tools/01-builtins.md) | 工具注册、类型、同步机制 |
| [core/community/00-overview.md](../core/community/00-overview.md) → [01-search-tools.md](../core/community/01-search-tools.md) | 社区工具模式：@tool 装饰器 + config |
| [core/mcp/00-overview.md](../core/mcp/00-overview.md) → [04-tools.md](../core/mcp/04-tools.md) | MCP 集成：懒加载 + mtime 缓存 + OAuth |

### 方向 B：记忆系统

| 文档 | 主题 |
|------|------|
| [core/memory/01-memory-system.md](../core/memory/01-memory-system.md) | 记忆系统全貌 |
| [core/memory/002-design-decisions.md](../core/memory/002-design-decisions.md) | 设计决策（最佳参考） |
| [core/memory/003-implementation-analysis.md](../core/memory/003-implementation-analysis.md) | 实现分析 |
| [lifecycle/03-memory-context-chain.md](../lifecycle/03-memory-context-chain.md) | 记忆上下文跨模块链路 |

### 方向 C：子代理与并发

| 文档 | 主题 |
|------|------|
| [core/subagents/00-overview.md](../core/subagents/00-overview.md) → [02-executor.md](../core/subagents/02-executor.md) | 双线程池架构、状态机 |
| [lifecycle/05-subagent-dispatch.md](../lifecycle/05-subagent-dispatch.md) | 子代理派发跨模块链路 |
| [core/runtime/04-concurrency-control.md](../core/runtime/04-concurrency-control.md) | 运行时并发控制 |

### 方向 D：沙箱与安全

| 文档 | 主题 |
|------|------|
| [core/sandbox/02-tools.md](../core/sandbox/02-tools.md) → [03-security.md](../core/sandbox/03-security.md) | 沙箱工具、安全机制 |
| [core/community/03-aio-sandbox.md](../core/community/03-aio-sandbox.md) | AIO Docker 沙箱实现 |
| [core/guardrails/01-overview.md](../core/guardrails/01-overview.md) | 工具调用前置授权 |

### 方向 E：技能与提示词

| 文档 | 主题 |
|------|------|
| [core/skills/01-overview.md](../core/skills/01-overview.md) → [04-features-and-policies.md](../core/skills/04-features-and-policies.md) | 技能发现、解析、工具策略 |
| [lifecycle/04-skills-loading-chain.md](../lifecycle/04-skills-loading-chain.md) | 技能加载跨模块链路 |

### 方向 F：Gateway 与 IM 频道

| 文档 | 主题 |
|------|------|
| [core/gateway/00-overview.md](../core/gateway/00-overview.md) → [03-middleware.md](../core/gateway/03-middleware.md) | FastAPI Gateway、CORS、CSRF |
| [core/channels/01-overview.md](../core/channels/01-overview.md) → [04-features-and-policies.md](../core/channels/04-features-and-policies.md) | IM 频道：飞书、Slack、Telegram、钉钉 |

---

## 第四层：设计模式与架构思想（~1.5 小时）

**目标**: 从具体实现中提炼可迁移的架构知识。

**前置要求**: 第二层 + 至少一个第三层方向

| 顺序 | 文档 | 主题 |
|------|------|------|
| 10 | [guides/01-architecture-decisions.md](01-architecture-decisions.md) | 重读，结合源码验证每个决策 |
| 11 | [guides/03-replaceability-map.md](03-replaceability-map.md) | 区分核心抽象、可复用组件、可替换实现 |
| 12 | [guides/02-extension-guide.md](02-extension-guide.md) | "我想加一个 X" 的操作指南 |

**学完后你能回答**:
- 如果我要构建自己的 Agent 系统，哪些设计可以复用？
- DeerFlow 的哪些决策是通用的，哪些是特定场景的？
- 如何在自己的项目中应用中间件链、虚拟路径、配置驱动等模式？

---

## 调试时读什么

遇到具体问题时，按以下路径定位：

| 问题类型 | 先读 | 再读 |
|---------|------|------|
| Agent 行为异常 | lifecycle/01 | core/agent/05-middlewares |
| 上下文被截断 | lifecycle/02 | core/memory/002-design-decisions |
| 记忆没更新 | lifecycle/03 | core/memory/01 + queue.py 源码 |
| 工具调用失败 | core/tools/00 | 社区工具对应源码 |
| 子代理卡住 | lifecycle/05 | core/subagents/02-executor |
| 文件上传失败 | lifecycle/06 | core/uploads/00 |
| MCP 工具不可用 | core/mcp/00 | core/mcp/01-cache |
| 模型调用失败 | core/models/00 | core/models/02-providers |
| 沙箱路径错误 | core/sandbox/01 | core/sandbox/03-security |
| 新增功能卡住 | guides/02 | 对应模块的 core/ 文档 |

---

## 快速参考

**核心源码入口**（按调用链排列）:

```
HTTP 请求入口     app/gateway/routers/thread_runs.py → stream_run()
运行管理         app/gateway/services.py → start_run()
Agent 构建       deerflow/agents/lead_agent/agent.py → make_lead_agent()
中间件装配       deerflow/agents/lead_agent/agent.py → _build_middlewares()
系统提示词       deerflow/agents/lead_agent/prompt.py → apply_prompt_template()
工具加载         deerflow/tools/tools.py → get_available_tools()
模型工厂         deerflow/models/factory.py → create_chat_model()
配置系统         deerflow/config/app_config.py → AppConfig
记忆存储         deerflow/agents/memory/storage.py → MemoryStorage
子代理执行       deerflow/subagents/executor.py → SubagentExecutor
SSE 流推送       deerflow/runtime/stream_bridge/ → StreamBridge
```
