# 01 - 运行时全局概览

## 运行时是什么

DeerFlow 的运行时是一套**在后台执行 Agent 图并实时推送事件的系统**。当前端发送一条消息给 Agent 时，运行时负责：

1. 管理这次运行的完整生命周期（创建 → 执行 → 完成/失败/取消）
2. 将 Agent 执行过程中的事件（AI 回复、工具调用、状态变化）实时推送给前端
3. 记录运行过程中的所有事件用于历史回放
4. 处理并发请求和取消操作

## 组件清单

运行时由以下核心组件构成，每个组件解决一个明确的问题：

### RunManager —— 运行注册表

**解决的问题**：需要一个地方知道"当前有哪些运行在进行中"，以及每个运行处于什么状态。

**做了什么**：
- 维护一个内存字典，记录每个运行的 ID、状态、所属线程、关联的 asyncio Task
- 提供创建、查询、状态变更、取消等操作
- 所有操作用 asyncio Lock 保护，防止并发修改
- 可选地连接持久化存储（RunStore），使运行历史在进程重启后仍然存在

**能力来源**：`deerflow.runtime.runs.manager`

### RunRecord —— 单次运行的数据载体

**解决的问题**：每次运行需要一个可变的数据结构来跟踪实时状态。

**包含的信息**：
- run_id、thread_id：唯一标识
- status：当前状态（pending → running → success/error/interrupted）
- abort_event：一个 asyncio.Event，用于通知运行"有人要求你停下来"
- abort_action：取消时要执行的动作（interrupt 还是 rollback）
- task：关联的 asyncio.Task，用于强制取消

**能力来源**：`deerflow.runtime.runs.manager`（RunRecord dataclass）

### StreamBridge —— 事件流管道

**解决的问题**：Agent 在后台线程执行，前端通过 SSE（Server-Sent Events）接收实时更新。需要一个管道连接这两端。

**做了什么**：
- 生产者侧（run_agent）：调用 `bridge.publish(run_id, event_name, data)` 把事件放入管道
- 消费者侧（Gateway SSE 端点）：调用 `bridge.subscribe(run_id)` 拿到一个异步迭代器，逐个消费事件
- 维护每个 run_id 的有界事件缓冲区（默认 256 条），支持客户端断线重连后从 Last-Event-ID 恢复
- 在没有事件时自动发送心跳，防止 SSE 连接因超时被代理服务器断开

**能力来源**：`deerflow.runtime.stream_bridge`（抽象接口）和 `deerflow.runtime.stream_bridge.memory`（内存实现）

### RunJournal —— 事件追踪器

**解决的问题**：需要记录运行过程中发生了什么——哪些 LLM 被调用、消耗了多少 Token、工具调用的输入输出是什么——这些信息用于历史回放和分析。

**做了什么**：
- 实现 LangChain 的 BaseCallbackHandler 接口，作为回调注入 Agent 执行过程
- 在 LLM 调用开始/结束、工具调用、链执行开始/结束时被自动回调
- 将回调数据标准化为结构化事件，缓冲写入 RunEventStore
- 在内存中累积 Token 使用量，按调用者分类（主 Agent、子 Agent、中间件）
- 运行结束时，将汇总数据（Token 数量、消息数、首条人类消息、最后 AI 消息）写入 RunStore

**能力来源**：`deerflow.runtime.journal`

### RunStore —— 运行元数据持久化

**解决的问题**：进程重启后运行历史不丢失，以及列表页查询不需要从事件流重建数据。

**做了什么**：
- 定义运行元数据的存储接口（put、get、list_by_thread、update_status 等）
- 内存实现（MemoryRunStore）：开发测试用
- SQL 实现（RunRepository）：生产环境用，写入 SQLite 或 PostgreSQL

**能力来源**：`deerflow.runtime.runs.store.base`（接口）、`deerflow.runtime.runs.store.memory`（内存实现）、`deerflow.persistence.run.sql`（SQL 实现）

### RunEventStore —— 事件持久化

**解决的问题**：运行的历史消息、工具调用、生命周期事件需要持久化，支持消息回放和分页查询。

**做了什么**：
- 存储每个事件的类型、内容、分类（message/trace/lifecycle）、元数据
- 支持按线程、按运行、按分类查询
- 支持分页（基于 seq 序号的游标分页）

**能力来源**：`deerflow.runtime.events.store`（多种实现：内存、JSONL 文件、SQL 数据库）

### Checkpointer —— 状态检查点

**解决的问题**：Agent 的对话状态需要在多轮对话间持久化。用户发送第二条消息时，Agent 需要记得第一条消息的上下文。

**做了什么**：
- 由 LangGraph 提供，不是 DeerFlow 自己实现的
- 在每个图节点执行完成后自动保存状态快照
- 支持回滚到之前的状态快照（用于取消操作中的 rollback 策略）

**能力来源**：LangGraph 框架（`langgraph.checkpoint`）

## 数据流方向

```
前端请求
  │
  ▼
Gateway API（thread_runs 路由）
  │
  ├──▶ RunManager.create_or_reject()  ← 创建运行记录，处理并发策略
  │
  ├──▶ asyncio.create_task(run_agent(...))  ← 在后台 Task 中启动 Agent
  │
  │    ┌──────────────────────────────────────────────┐
  │    │ run_agent() 在后台执行                         │
  │    │                                                │
  │    │  1. 初始化 RunJournal（事件追踪器）              │
  │    │  2. 捕获运行前检查点快照（用于回滚）              │
  │    │  3. 发布 metadata 事件到 StreamBridge           │
  │    │  4. 构建 Agent 图 + 注入运行时上下文             │
  │    │  5. 调用 agent.astream() 流式执行               │
  │    │     ├── LangGraph 执行图节点                    │
  │    │     ├── RunJournal 通过回调捕获事件              │
  │    │     └── 每个 chunk 通过 bridge.publish() 推送   │
  │    │  6. 处理完成/取消/错误                          │
  │    │  7. 刷新事件缓冲区 + 写入完成数据                │
  │    │  8. 同步标题到线程元数据                        │
  │    │  9. 发送 end 事件 + 延迟清理资源                │
  │    └──────────────────────────────────────────────┘
  │
  └──▶ bridge.subscribe(run_id)  ← SSE 端点消费事件，推送给前端
```

## 为什么这样设计

### 为什么 RunManager 是内存注册表而不是纯数据库查询

Agent 运行是**进程内**的——一个运行对应一个 asyncio Task，这个 Task 持有 abort_event、task 引用等**无法序列化**的运行时状态。数据库只能存静态元数据，无法存"怎么通知正在运行的 Agent 停下来"。所以 RunManager 必须是内存注册表，数据库只是持久化层。

### 为什么 StreamBridge 不是简单的队列

前端的 SSE 连接可能断线重连。StreamBridge 保留了一个有界的事件缓冲区（默认 256 条），并给每个事件分配递增 ID。当客户端用 Last-Event-ID 重连时，StreamBridge 从该 ID 之后重放缓冲的事件。这是兼容 LangGraph Platform 协议的必要设计。

### 为什么 RunJournal 用 LangChain 回调而不是自己插桩

LangChain/LangGraph 的回调机制是框架级的钩子——无论 Agent 执行多少层嵌套的链、子 Agent、工具调用，回调都会被触发。如果自己插桩，需要在每个可能的位置手动添加追踪代码，既容易遗漏也难以维护。通过实现 BaseCallbackHandler 接口，RunJournal 可以无侵入地捕获所有层级的执行事件。
