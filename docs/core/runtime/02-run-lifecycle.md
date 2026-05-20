# 02 - 单次运行的完整生命周期

本文档描述一次 Agent 运行从请求到结束的每一步，以 `run_agent()` 函数为核心主线。

## 阶段一：创建运行记录

**触发者**：Gateway API 收到前端请求（如 `POST /api/threads/{id}/runs/stream`）

**做了什么**：
1. 解析请求参数：线程 ID、助手 ID、多任务策略、模型名称、流模式等
2. 调用 `RunManager.create_or_reject()`：
   - **检查该线程是否有进行中的运行**（pending 或 running 状态）
   - 根据多任务策略决定是拒绝、中断还是回滚已有运行
   - 创建一条新的 RunRecord，状态为 pending
   - 如果有持久化存储，将记录写入数据库
3. 运行记录创建成功后，在后台 Task 中启动 `run_agent()`

**为什么这样做**：
多任务策略是运行时并发控制的核心。同一线程上的运行共享对话状态，如果两个运行同时执行，会导致状态冲突。在创建阶段就解决冲突，而不是在执行阶段，是因为执行开始后再中断更复杂且有副作用。

## 阶段二：初始化（run_agent 进入）

### 2.1 初始化事件追踪器

**做了什么**：
- 创建 RunJournal 实例，传入 run_id、thread_id、event_store
- RunJournal 是 LangChain 的 BaseCallbackHandler，后续会作为回调注入 Agent 执行过程

**为什么这样做**：
RunJournal 需要在 Agent 开始执行前就创建好，这样从第一个 LLM 调用到最后一个工具调用的所有事件都能被捕获。如果延迟创建，会丢失 Agent 启动阶段的事件。

### 2.2 标记运行状态为 running

**做了什么**：
- 调用 `RunManager.set_status(run_id, RunStatus.running)`
- 更新内存记录的状态和 updated_at 时间戳
- 如果有持久化存储，同步更新数据库

**为什么这样做**：
状态转换是渐进式的（pending → running → success/error/interrupted），每一步都持久化。这样即使进程崩溃，重启后也能通过数据库恢复运行的状态（至少知道它开始了但没完成）。

### 2.3 捕获运行前检查点快照

**做了什么**：
- 通过 LangGraph 的 Checkpointer 获取当前线程最新的检查点
- 深拷贝检查点数据（checkpoint 元数据、pending_writes）保存到变量中
- 如果获取失败，记录警告但不中断运行

**为什么这样做**：
这是**回滚策略**的基础。如果用户取消运行并选择 rollback，运行时需要将线程状态恢复到本次运行开始之前。如果不提前保存快照，取消时就无法知道要恢复到哪个状态。

**为什么允许获取失败继续运行**：
检查点获取可能因为数据库连接问题等暂时性错误失败。回滚只是取消的一种模式（另一种是 interrupt，不回滚），不应该因为快照获取失败就阻止正常的 Agent 执行。

### 2.4 发布 metadata 事件

**做了什么**：
- 调用 `bridge.publish(run_id, "metadata", {run_id, thread_id})`
- 这是 StreamBridge 上的第一个事件

**为什么这样做**：
前端的 `useStream` React Hook 需要 run_id 和 thread_id 来建立 SSE 连接的上下文。这个 metadata 事件是 LangGraph Platform 协议规定的第一个事件。

## 阶段三：构建 Agent

### 3.1 构建运行时上下文

**做了什么**：
- 将 thread_id、run_id 注入到 LangGraph 的 Runtime context 中
- 如果调用者提供了额外的上下文（如自定义 Agent 名称），也一并注入
- 将 AppConfig 实例注入上下文，让工具可以不通过全局变量获取配置

**为什么这样做**：
Agent 执行过程中的中间件和工具（如 ThreadDataMiddleware、Sandbox 工具）需要知道当前线程和运行的信息来正确工作。LangGraph 的 Runtime.context 是传递这些信息的标准机制。如果不注入，中间件就无法创建正确的线程目录、工具就无法知道要操作哪个沙箱。

### 3.2 注入 RunJournal 回调

**做了什么**：
- 将 RunJournal 实例添加到 LangChain 的 callbacks 列表中
- 后续 Agent 执行过程中，LangChain 会在每个 LLM 调用、工具调用、链执行时回调 RunJournal

**为什么这样做**：
LangChain 的回调机制要求在配置中注入回调处理器。RunJournal 作为 BaseCallbackHandler，通过这种方式无侵入地接入执行流程。

### 3.3 调用 Agent 工厂

**做了什么**：
- 调用 `agent_factory(config=runnable_config)` 或 `agent_factory(config=runnable_config, app_config=app_config)`
- 工厂函数通常是 `make_lead_agent()`，它构建完整的 LangGraph StateGraph
- 工厂函数内部会：加载模型、注册工具、组装中间件链、编译图

**为什么这样做**：
每次运行都重新构建 Agent，而不是复用一个全局实例。这是因为每次运行可能有不同的配置（不同模型、不同工具集、不同运行时参数）。通过工厂模式，每次运行都能获得完全定制的 Agent。

### 3.4 修正模型名称

**做了什么**：
- 检查 Agent 实际使用的模型名称是否与请求的不同
- 如果请求了 "gpt-4" 但 Agent 的配置可能将其映射为 "gpt-4-turbo"，则更新记录

**为什么这样做**：
模型名称的解析发生在 Agent 工厂内部（可能有默认模型回退、名称映射等逻辑）。运行记录中应该保存实际使用的模型名称，而不是用户请求的名称，这对成本分析和统计很重要。

### 3.5 挂载 Checkpointer 和 Store

**做了什么**：
- 将 Checkpointer 实例设置到 Agent 上（`agent.checkpointer = checkpointer`）
- 将 Store 实例设置到 Agent 上（`agent.store = store`）

**为什么这样做**：
Checkpointer 负责持久化对话状态（多轮对话的上下文），Store 负责持久化线程元数据。这两个实例由 Gateway 启动时创建，每次运行时挂载到 Agent 上。

### 3.6 设置中断节点

**做了什么**：
- 如果请求指定了 `interrupt_before` 或 `interrupt_after`，将其设置到 Agent 上

**为什么这样做**：
这是 LangGraph 的人机交互（Human-in-the-loop）机制。某些工具调用（如执行危险操作）需要在执行前暂停，等待人类确认后继续。中断节点告诉 LangGraph 在哪些节点前后暂停执行。

## 阶段四：流式执行 Agent

### 4.1 构建 stream_mode 列表

**做了什么**：
- 将前端请求的流模式转换为 LangGraph 的 stream_mode
- "messages-tuple" 映射为 LangGraph 的 "messages" 模式
- "events" 模式被跳过（不支持，因为需要 astream_events 而非 astream）
- 如果最终没有有效模式，默认使用 "values"

**为什么这样做**：
LangGraph 的 `astream()` 方法支持多种流模式，每种模式产生不同格式的输出。前端根据需求选择模式——需要实时显示打字效果选 "messages"，需要完整状态快照选 "values"，需要增量更新选 "updates"。

### 4.2 流式迭代

**做了什么**：
- 调用 `agent.astream(graph_input, config=..., stream_mode=lg_modes)`
- 对每个返回的 chunk：
  1. 检查 abort_event 是否被设置（有人要求取消？）
  2. 如果是，立即停止迭代
  3. 如果否，将 chunk 序列化后通过 `bridge.publish()` 推送给前端

**为什么在每次迭代检查取消**：
Agent 执行可能持续数分钟（思考、调用工具、等待子 Agent 完成）。在每次迭代检查取消信号，使得取消请求能在秒级生效，而不是等整个运行自然结束。

**为什么单模式和多模式的处理不同**：
LangGraph 的 astream 在单模式时直接返回 chunk，在多模式时返回 (mode, chunk) 元组。区分处理避免了不必要的解包开销。

## 阶段五：处理完成

### 5.1 正常完成

**做了什么**：
- 设置运行状态为 `success`
- RunJournal 将所有缓冲事件刷新到 RunEventStore
- 将 Token 使用量汇总数据写入 RunStore

### 5.2 被取消

**做了什么**：
- 如果动作是 `interrupt`：设置状态为 `interrupted`，保留当前检查点
- 如果动作是 `rollback`：设置状态为 `error`（标记为回滚），然后恢复运行前的检查点快照

**回滚的具体操作**：
1. 创建一个新的空检查点标记（新 ID + 时间戳）
2. 将保存的运行前检查点数据写入 Checkpointer
3. 恢复运行前的 pending_writes（那些已提交但未应用的写入）

### 5.3 异常

**做了什么**：
- 捕获异常信息
- 设置运行状态为 `error`，附带错误信息
- 通过 StreamBridge 发布 error 事件给前端

## 阶段六：收尾（finally 块）

无论运行是成功、失败还是被取消，以下操作都会执行：

### 6.1 刷新事件缓冲区

**做了什么**：
- 调用 `journal.flush()` 确保所有缓冲事件写入 RunEventStore
- 获取 RunJournal 累积的完成数据（Token 使用量、消息数等）
- 调用 `run_manager.update_run_completion()` 将汇总数据写入 RunStore

**为什么在 finally 中刷新**：
回调是同步的，可能在工作线程中触发。如果运行被取消或异常中断，缓冲区中可能还有未写入的事件。在 finally 中强制刷新确保不丢失任何数据。

### 6.2 同步标题

**做了什么**：
- 从 Checkpointer 读取当前线程的检查点
- 如果检查点中有 title 字段，更新 threads_meta 表的 display_name

**为什么这样做**：
标题是由 TitleMiddleware 在 Agent 执行过程中自动生成的，存储在 LangGraph 的检查点中。但前端列表页查询的是 threads_meta 表。需要在运行结束时将标题从检查点同步到 threads_meta，否则列表页看不到标题。

### 6.3 更新线程状态

**做了什么**：
- 如果运行成功，设置线程状态为 `idle`（空闲，可以接受新消息）
- 如果运行失败，设置线程状态为错误状态值

### 6.4 关闭流和清理

**做了什么**：
- 调用 `bridge.publish_end(run_id)` 通知 SSE 消费者"没有更多事件了"
- 启动延迟清理任务（60 秒后清除 StreamBridge 中该 run_id 的缓冲区）

**为什么延迟清理**：
客户端可能在收到最后一个事件后还需要几秒钟处理数据（渲染 UI 等）。60 秒的延迟给客户端足够的缓冲时间，也支持在此期间通过 Last-Event-ID 重连获取最后几个事件。
