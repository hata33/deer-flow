# 03 - 运行时能力清单与来源

本文档描述 `run_agent()` 使用的每一项能力——它是什么、来自哪里、为什么需要它。

---

## 能力 1：StreamBridge —— 实时事件推送管道

### 做了什么

run_agent 调用 `bridge.publish(run_id, event_name, data)` 将 Agent 执行过程中的每个事件推入管道。Gateway 的 SSE 端点通过 `bridge.subscribe(run_id)` 消费这些事件，实时发送给前端。

### 能力来源

| 层级 | 模块 | 说明 |
|------|------|------|
| 抽象接口 | `deerflow.runtime.stream_bridge.base.StreamBridge` | 定义 publish/publish_end/subscribe/cleanup 四个核心方法 |
| 内存实现 | `deerflow.runtime.stream_bridge.memory.MemoryStreamBridge` | 基于进程内 asyncio.Condition + 事件列表的实现 |
| 创建入口 | Gateway 启动时创建单例 | 在 `app/gateway/routers/deps.py` 中初始化，全局共享 |

### 为什么需要它

如果直接在 Agent 执行循环中写 SSE 响应，那 Agent 的执行和 HTTP 响应就耦合在一起了——如果客户端断线，Agent 就无法继续执行。StreamBridge 将**事件的生产**（Agent 执行）和**事件的消费**（SSE 推送）解耦到两个独立的异步任务中。

解耦带来的好处：
- Agent 可以在客户端断线后继续执行（配合 DisconnectMode.continue 模式）
- 多个客户端可以同时订阅同一个运行的事件
- 事件有缓冲区，支持断线重连后从上次位置恢复

---

## 能力 2：RunManager —— 运行注册表和状态管理

### 做了什么

run_agent 通过 RunManager 完成：
- `set_status(run_id, running)` → 标记运行开始
- `update_model_name(run_id, name)` → 修正实际使用的模型名
- `set_status(run_id, success/error/interrupted)` → 标记运行结果
- `update_run_completion(run_id, ...)` → 写入 Token 使用汇总

### 能力来源

| 层级 | 模块 | 说明 |
|------|------|------|
| 核心实现 | `deerflow.runtime.runs.manager.RunManager` | 内存注册表 + 可选持久化 |
| 持久化接口 | `deerflow.runtime.runs.store.base.RunStore` | 抽象存储接口 |
| 内存实现 | `deerflow.runtime.runs.store.memory.MemoryRunStore` | 字典实现 |
| SQL 实现 | `deerflow.persistence.run.sql.RunRepository` | SQLAlchemy 实现 |

### 为什么需要它

Agent 的运行是进程内的 asyncio Task。这个 Task 持有无法序列化的状态（如 abort_event、task 引用）。RunManager 是唯一知道"哪些运行正在进行、如何控制它们"的组件。没有它：
- 无法知道某个 run_id 是否正在执行
- 无法向正在执行的运行发送取消信号
- 无法在进程重启后恢复运行历史

### 双层存储的设计

RunManager 采用**内存为主、数据库为辅**的双层存储：
- 内存记录包含运行时状态（task 引用、abort_event）—— 不可序列化
- 数据库记录包含可序列化的元数据（状态、时间、Token 用量）—— 持久化

查询时，先查内存；如果内存没有（比如进程重启后），回退到数据库重建一个只读记录。合并两者结果时，内存记录优先（因为它的数据更新鲜）。

---

## 能力 3：Checkpointer —— 对话状态持久化

### 做了什么

run_agent 使用 Checkpointer 做两件事：
1. **运行前**：获取最新检查点快照，用于回滚
2. **挂载到 Agent**：让 Agent 在每个节点执行后自动保存状态

### 能力来源

| 层级 | 模块 | 说明 |
|------|------|------|
| 框架提供 | `langgraph.checkpoint.base.BaseCheckpointSaver` | LangGraph 的检查点抽象 |
| 内存实现 | `langgraph.checkpoint.memory.MemorySaver` | 开发测试用 |
| SQL 实现 | `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` 等 | 生产环境用 |

### 为什么需要它

多轮对话需要状态持久化。用户发第一条消息时，Agent 的回复写入检查点；用户发第二条消息时，Agent 从检查点恢复上下文继续对话。没有检查点，每次对话都是独立的，Agent 无法"记得"之前的交互。

回滚功能也依赖检查点——将检查点恢复到运行前的状态，就像这次运行从未发生过一样。

---

## 能力 4：RunJournal —— LangChain 回调驱动的事件追踪器

### 做了什么

run_agent 创建 RunJournal 并作为回调注入 Agent 执行过程。RunJournal 在以下时机被自动回调：
- LLM 开始调用 → 记录开始时间、提取用户输入
- LLM 调用结束 → 记录 AI 回复、累积 Token 使用量
- 工具调用结束 → 记录工具输出
- 链开始/结束 → 记录运行生命周期事件

### 能力来源

| 层级 | 模块 | 说明 |
|------|------|------|
| 回调接口 | `langchain_core.callbacks.BaseCallbackHandler` | LangChain 的回调协议 |
| 具体实现 | `deerflow.runtime.journal.RunJournal` | DeerFlow 的事件追踪实现 |
| 存储接口 | `deerflow.runtime.events.store.base.RunEventStore` | 事件存储抽象 |
| 存储实现 | `deerflow.runtime.events.store.jsonl` / `db` | JSONL 文件或 SQL 数据库 |

### 为什么需要它

运行结束后需要回答以下问题：
- 这次运行消耗了多少 Token？花了多少钱？
- Agent 调用了哪些工具？输入输出是什么？
- 中间发生了什么？用户发了什么、AI 回复了什么？

RunJournal 通过 LangChain 的回调机制无侵入地捕获这些信息。不需要在 Agent 代码中添加任何追踪逻辑——只要 RunJournal 作为回调被注入，所有 LLM 调用、工具调用、链执行的事件都会被自动记录。

### Token 去重的设计

LangChain 可能对同一个 LLM 调用多次触发 on_llm_end 回调（比如在流式和非流式两个路径上）。RunJournal 通过跟踪已处理的 run_id 来去重，确保同一个 LLM 调用的 Token 只计数一次。

---

## 能力 5：Agent 工厂 —— 动态构建 Agent 图

### 做了什么

run_agent 调用 `agent_factory(config=...)` 获取一个编译好的 LangGraph StateGraph。这个工厂函数（通常是 `make_lead_agent()`）内部完成：
- 根据配置选择 LLM 模型
- 加载所有可用工具（内置、MCP、社区、子 Agent）
- 组装 18 层中间件链
- 编译为可执行的 StateGraph

### 能力来源

| 层级 | 模块 | 说明 |
|------|------|------|
| 工厂函数 | `deerflow.agents.lead_agent.agent.make_lead_agent` | 主 Agent 的构建入口 |
| 图定义 | LangGraph StateGraph API | 定义节点和边 |
| 中间件 | `deerflow.agents.middlewares` | 18 个中间件组件 |
| 工具加载 | `deerflow.tools` | 内置 + MCP + 社区工具 |

### 为什么每次运行都重建 Agent

不同运行可能使用不同的模型、不同的工具集、不同的运行时配置（如是否启用计划模式、是否启用子 Agent）。工厂模式使得每次运行都能获得完全定制的 Agent，而不是使用一个"最大公约数"的全局实例。

---

## 能力 6：Runtime Context —— 工具和中间件的运行时信息

### 做了什么

run_agent 构建一个包含 thread_id、run_id、app_config 的上下文字典，注入到 LangGraph 的 Runtime 中。工具和中间件通过 `ToolRuntime.context` 访问这些信息。

### 能力来源

| 层级 | 模块 | 说明 |
|------|------|------|
| 注入机制 | `langgraph.runtime.Runtime` | LangGraph 的运行时上下文容器 |
| 上下文构建 | `deerflow.runtime.runs.worker._build_runtime_context` | 组装上下文字典 |

### 为什么需要它

工具和中间件在执行时需要知道"我在为哪个线程服务"、"当前运行是什么"。例如：
- ThreadDataMiddleware 需要知道 thread_id 来创建线程专属目录
- Sandbox 工具需要知道 thread_id 来创建线程专属沙箱
- Memory 中间件需要知道 user_id 来读取用户的记忆文件

这些信息通过 Runtime Context 传递，而不是通过全局变量，使得运行时可以在同一进程中安全地为多个并发请求服务。

---

## 能力 7：RunStore —— 运行元数据持久化

### 做了什么

run_agent 通过 RunManager 间接使用 RunStore：
- 运行创建时写入元数据
- 状态变更时更新数据库
- 运行完成时写入 Token 使用汇总和便利字段

### 能力来源

| 层级 | 模块 | 说明 |
|------|------|------|
| 抽象接口 | `deerflow.runtime.runs.store.base.RunStore` | 定义持久化操作 |
| 内存实现 | `deerflow.runtime.runs.store.memory.MemoryRunStore` | 开发测试用 |
| SQL 实现 | `deerflow.persistence.run.sql.RunRepository` | 写入 SQLite 或 PostgreSQL |

### 便利字段的设计

runs 表中有三个"便利字段"：message_count、first_human_message、last_ai_message。这些数据实际上可以从 run_events 表中查询得到，但单独存储是因为：
- 列表页展示需要这些信息，如果每次都从事件表聚合查询，性能很差
- 这些字段在运行完成时一次性写入，读取时不需要额外查询

---

## 能力 8：序列化器 —— LangGraph 数据到 JSON 的转换

### 做了什么

run_agent 在发布每个事件前调用 `serialize(chunk, mode=mode)` 将 LangGraph 的内部数据结构转换为 JSON 可序列化的格式。

### 能力来源

`deerflow.runtime.serialization` 模块，处理 LangGraph 特有的数据类型（如 LangChain Message 对象、检查点引用等）。

### 为什么需要它

LangGraph 的 astream 返回的 chunk 包含 LangChain 的 Message 对象、状态字典等，这些不是 JSON 可序列化的。SSE 协议要求所有数据都是 JSON 字符串。序列化器负责这个转换，包括处理消息中的工具调用、多媒体内容等复杂结构。
