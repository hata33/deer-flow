# Agent 分层架构设计

**问题**: Agent 系统涉及前端、API、编排、运行时、模型、工具、沙箱、配置等模块，如果模块之间没有清晰的边界和依赖规则，系统会迅速退化为大泥球。

---

## 问题 1：DeerFlow 的分层架构是什么？

八层架构，依赖方向严格自上而下：

```
┌─────────────────────────────┐
│ 1. Frontend Layer           │  Next.js + SSE 流式渲染
├─────────────────────────────┤
│ 2. Gateway API Layer        │  FastAPI + 认证 + 路由
├─────────────────────────────┤
│ 3. Agent Orchestration      │  LangGraph + 20 中间件
├─────────────────────────────┤
│ 4. Runtime Layer            │  Run 管理 + 流式 + 持久化
├─────────────────────────────┤
│ 5. Model Layer              │  LLM 抽象 + 工厂模式
├─────────────────────────────┤
│ 6. Tool Layer               │  五源工具装配
├─────────────────────────────┤
│ 7. Sandbox Layer            │  执行环境隔离
├─────────────────────────────┤
│ 8. Configuration Layer      │  YAML + Pydantic + 反射
└─────────────────────────────┘
         ↑ 横向被所有层访问
```

**核心规则**：上层可以依赖下层，下层不能依赖上层。配置层是横向的，被所有层访问。

---

## 问题 2：为什么不能用 MVC 或简单三层？

传统三层（表现层-业务层-数据层）不适用于 Agent 系统：

| 维度 | 传统 Web | Agent 系统 |
|------|---------|-----------|
| 请求模式 | 请求-响应 | 流式 SSE |
| 执行路径 | 确定性 | 不确定（LLM 决策） |
| 状态管理 | 无状态 | 长对话+检查点 |
| 外部依赖 | 数据库 | LLM API + 工具 + 沙箱 |
| 并发模型 | 线程池 | 混合（asyncio + 线程） |

Agent 系统需要专门的层次来处理流式推送、工具编排、沙箱隔离等。

---

## 问题 3：Agent 编排层为什么是核心？

```
Agent Orchestration（第 3 层）是系统枢纽：

上层: Gateway 传入请求
下层: 调用 Model、Tool、Sandbox
横向: 读取 Configuration
```

这一层负责：
- 20 个中间件的装配和执行
- ReAct 循环（思考→行动→观察→...）
- 上下文管理（记忆、技能、压缩）
- 错误处理和恢复

它是**唯一的协调者**——其他层不需要知道彼此的存在。

---

## 问题 4：层间通信怎么设计？

| 层间通信 | 方式 | 原因 |
|---------|------|------|
| Frontend ↔ Gateway | HTTP + SSE | 标准化，前端无状态 |
| Gateway → Runtime | 函数调用（同进程） | 性能 |
| Runtime → Agent | 函数调用 | LangGraph API |
| Agent → Model | LangChain BaseChatModel | 抽象多 Provider |
| Agent → Tool | LangChain BaseTool | 统一工具接口 |
| Agent → Sandbox | SandboxProvider 接口 | 可替换实现 |

**关键原则**：跨层通信通过接口，不通过具体实现。这让每一层都可以独立替换。

---

## 问题 5：配置层为什么是横向的？

配置被所有层访问：

```yaml
# 模型配置 → Model Layer 使用
models:
  default: "claude-sonnet-4-20250514"

# 护栏配置 → Agent Layer 使用
guardrails:
  enabled: true

# 沙箱配置 → Sandbox Layer 使用
sandbox:
  use: "local"
```

如果配置是某一层的一部分，其他层就无法访问。所以配置层独立存在，被所有层横向引用。

---

## 问题 6：如何防止层边界被破坏？

| 防护 | 机制 |
|------|------|
| 包结构 | 每层是独立的 Python 包（`agents/`, `runtime/`, `models/`） |
| 接口抽象 | 层间通信通过 Protocol/BaseClass，不导入具体实现 |
| 反射加载 | 通过配置字符串动态加载，不硬编码 `import` |
| 依赖注入 | 运行时注入依赖，不在构造时绑定 |

```python
# 好的做法：通过接口
class SandboxProvider(Protocol):
    def execute(self, command: str) -> str: ...

# 坏的做法：直接导入具体实现
from deerflow.sandbox.local.local_sandbox import LocalSandbox  # 耦合!
```

---

## 问题 7：前端和后端怎么解耦？

通过 SSE 事件协议：

```
前端不需要知道：
    - Agent 怎么构建
    - 中间件怎么排序
    - 工具怎么执行

前端只需要知道：
    - SSE 事件格式（values, messages-tuple, custom, end）
    - 消息类型（human, ai, tool, summary）
    - REST API 端点

后端不需要知道：
    - React 怎么渲染
    - 状态怎么管理
    - 用户在什么设备上
```

前后端唯一耦合点是 SSE 事件格式——这是一个稳定的接口。

---

## 问题 8：如何测试某一层而不依赖其他层？

| 层 | 测试策略 | Mock 对象 |
|----|---------|----------|
| Frontend | Mock SSE 事件 | 固定 JSON 事件流 |
| Gateway | Mock Agent | 固定 Run 结果 |
| Agent | Mock Model + Tool | 固定 LLM 输出 + 工具结果 |
| Runtime | Mock Checkpointer | 内存检查点 |
| Model | Mock HTTP | 录制/回放 LLM 响应 |
| Tool | Mock Sandbox | 内存文件系统 |
| Sandbox | 无需 Mock | 直接测试 |

每层都可以独立测试——这正是分层架构的最大价值。

---

## 问题 9：新增一个功能要改几层？

| 功能 | 涉及层 | 改动量 |
|------|--------|-------|
| 新工具 | Tool Layer + Config | 小 |
| 新中间件 | Agent Layer | 小 |
| 新 LLM Provider | Model Layer + Config | 小 |
| 新 API 端点 | Gateway + Frontend | 中 |
| 新沙箱实现 | Sandbox Layer + Config | 中 |
| 新通道 | Channel（新组件） | 大 |

**大部分新功能只改 1-2 层**，不影响其他层。

---

## 问题 10：分层架构的代价是什么？

| 代价 | 表现 | 应对 |
|------|------|------|
| 间接调用多 | 简单操作要穿多层 | 性能不敏感路径可接受 |
| 接口维护成本 | Protocol/ABC 需要同步 | 接口尽量少且稳定 |
| 调试链路长 | 请求跨多层，日志分散 | 追踪系统（LangSmith）串联 |
| 学习曲线陡 | 新人需要理解八层 | 分层文档 + 阅读路径 |

好的架构不是没有代价，而是代价可控且有回报。

---

## 数据流概览

```
用户请求
    │
    ▼ Frontend Layer
HTTP POST /api/threads/{id}/runs
    │
    ▼ Gateway Layer
认证 → 路由 → 创建 Run
    │
    ▼ Runtime Layer
获取锁 → 保存检查点 → 调度执行
    │
    ▼ Agent Layer
构建 Agent → 装配中间件 → ReAct 循环
    │
    ├── Model Layer → LLM API 调用
    ├── Tool Layer → 工具执行
    └── Sandbox Layer → 文件/命令隔离
    │
    ▼ Runtime Layer
StreamBridge → SSE 推送
RunJournal → 事件持久化
    │
    ▼ Frontend Layer
SSE 事件 → React 状态更新 → UI 渲染
```

---

## 源码位置

| 层 | 目录 |
|----|------|
| Frontend | `frontend/src/` |
| Gateway | `backend/app/gateway/` |
| Agent | `backend/packages/harness/deerflow/agents/` |
| Runtime | `backend/packages/harness/deerflow/runtime/` |
| Model | `backend/packages/harness/deerflow/models/` |
| Tool | `backend/packages/harness/deerflow/tools/` |
| Sandbox | `backend/packages/harness/deerflow/sandbox/` |
| Config | `backend/packages/harness/deerflow/config/` |

## 深入阅读

- [架构决策](../guides/01-architecture-decisions.md) — 设计选择
- [分层架构 Q&A](../Q&A/02-layered-architecture.md) — 问答
- [扩展指南](../guides/02-extension-guide.md) — 如何扩展各层
- [动态反射](021-动态反射与配置驱动.md) — 配置驱动架构
