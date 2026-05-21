# 07 - 运行时实例生命周期

本文档回答四个核心问题：RunJournal 是否每次重建、Agent 的创建/复用策略、多用户并发模型、中断执行的缓存与持久化机制。

## 1. 每次执行 Agent 都会重新创建 RunJournal 实例吗？

**是的，每次 run 都创建新的 RunJournal。**

在 `worker.py` 的 `run_agent()` 中（约第 209-217 行），每次调用都会通过 `RunJournal(...)` 创建新实例，传入本次的 `run_id` 和 `thread_id`：

```python
if event_store is not None:
    journal = RunJournal(
        run_id=run_id,
        thread_id=thread_id,
        event_store=event_store,
        track_token_usage=getattr(run_events_config, "track_token_usage", True),
    )
```

RunJournal 是 LangChain 的 `BaseCallbackHandler`，通过 LangChain 的 callbacks 机制注入 Agent 执行过程（`config.setdefault("callbacks", []).append(journal)`），用来捕获该次 run 的所有事件和 token 使用量。

### 为什么每次都新建

- 每次 run 有独立的 `run_id`，事件需要隔离到各自的 run 记录中
- Token 使用量统计是 per-run 的，累积数据不能跨 run 污染
- 运行结束后在 finally 块中 flush 并写入 RunStore，该 RunJournal 实例随函数返回被回收

### 生命周期

1. `run_agent()` 开始 → 创建 RunJournal
2. 注入 callbacks → Agent 执行过程中回调 RunJournal 记录事件
3. 运行结束（成功/失败/取消）→ finally 块中 `journal.flush()` 将缓冲事件写入 RunEventStore
4. `journal.get_completion_data()` 获取汇总数据（token 使用量、消息数等）写入 RunStore
5. RunJournal 实例随函数返回被 GC 回收

## 2. 每一轮新对话或中断重连，都会重新创建 Agent 吗？

### Gateway 模式（HTTP API）

**每次 run 都重新创建 Agent。**

在 `worker.py` 约第 270-272 行，通过 `agent_factory(config=...)` 创建：

```python
agent = agent_factory(config=runnable_config, app_config=ctx.app_config)
```

不缓存、不复用。每次 run 可能有不同配置（模型名称、thinking 模式、工具集等），工厂模式确保每次都获得完全定制的 Agent。工厂函数内部会：加载模型、注册工具、组装中间件链、编译 StateGraph。

### DeerFlowClient 模式（嵌入式 Python 客户端）

**Agent 不是每次都重建，而是按配置 key 缓存复用。**

`_ensure_agent()` 方法（`client.py` 约第 210-223 行）通过配置 key 判断是否需要重建：

```python
key = (
    cfg.get("model_name"),
    cfg.get("thinking_enabled"),
    cfg.get("is_plan_mode"),
    cfg.get("subagent_enabled"),
    self._agent_name,
    frozenset(self._available_skills) if self._available_skills is not None else None,
)

if self._agent is not None and self._agent_config_key == key:
    return  # 复用现有 agent
```

只有当模型名、thinking 模式、plan_mode、subagent、agent_name 或 skills 集合发生变化时才重建。调用 `reset_agent()` 可强制下次重建。

### 中断后的"重连"

**不是重连已中断的 Agent，而是创建一个全新的 run。**

中断的 run 的检查点状态被保留在 LangGraph Checkpointer 中（interrupt 策略保留、rollback 策略回滚）。新 run 从检查点恢复的上下文继续，但 Agent 实例本身是新建的。

```
用户发送消息 → 创建新 RunRecord → 创建新 RunJournal → 创建新 Agent
→ Agent.astream() → LangGraph 从 Checkpointer 加载上次检查点 → 从断点恢复执行
```

## 3. 多用户同时访问，会为每个用户创建不同的线程执行吗？

### 并发模型：asyncio 协程，非 OS 线程

系统使用 **asyncio**（而非操作系统线程）处理并发。所有用户的请求在同一个进程的同一个事件循环中通过协程并发执行。

核心设计：

- **RunManager** 是单例，内部用 `self._runs: dict[str, RunRecord]` 跟踪所有活跃的 run，用 `asyncio.Lock` 保护并发访问
- 每个 run 作为独立的 `asyncio.Task` 在事件循环中执行
- 不同 thread_id 的 run 可以真正并行执行（协程级并行）

### 用户隔离：user_id + thread_id

多个用户通过逻辑隔离而非进程/线程隔离：

| 隔离维度 | 实现方式 |
|----------|----------|
| 文件系统 | `backend/.deer-flow/users/{user_id}/threads/{thread_id}/user-data/{workspace,uploads,outputs}` |
| 对话状态 | LangGraph Checkpointer 按 `thread_id` 隔离检查点 |
| 记忆数据 | `{base_dir}/users/{user_id}/memory.json`（per-user） |
| Agent 配置 | `{base_dir}/users/{user_id}/agents/{agent_name}/`（per-user per-agent） |
| 运行记录 | RunManager 内存注册表 + 可选 RunStore 持久化 |
| Sandbox | per-thread 的 `LocalSandbox`，LRU 缓存（默认 256 条） |

### 并发控制

同一 `thread_id` 上的多个 run 通过多任务策略（reject/interrupt/rollback）互斥，但不同 thread_id 的 run 可以同时执行。`asyncio.Lock` 确保检查+操作的原子性，消除 TOCTOU 竞争。

### 为什么不用 OS 线程

Agent 执行主要是 I/O 密集型（等待 LLM 响应、工具调用），asyncio 协程在 I/O 等待时自动让出控制权，比 OS 线程更轻量、更高效。单进程即可支撑大量并发用户。

## 4. 中断的执行是一直缓存在内存吗？

RunManager 维护内存注册表 `self._runs: dict[str, RunRecord]`，同时可选地持久化到 `RunStore`（数据库）。中断后的数据保留取决于中断类型和持久化配置。

### 三种中断场景

#### (a) 用户手动中断暂停（前端发送 cancel 请求）

调用 `RunManager.cancel()`：

1. 设置 `abort_event` → 通知 `run_agent()` 停止迭代
2. 取消 `asyncio.Task`
3. 状态标记为 `interrupted`
4. 持久化状态到 RunStore（如果配置了）
5. 根据 `abort_action` 处理检查点：
   - **interrupt**：保留当前检查点（Agent 部分工作成果保留）
   - **rollback**：回滚到运行前的检查点快照（就像运行从未发生过）

内存中的 RunRecord 在 `cleanup(run_id, delay=300)` 后移除（默认 5 分钟）。

#### (b) 用户退出/关闭页面/网络异常

通过 `on_disconnect` 配置控制，在创建 run 时由前端指定：

```python
class DisconnectMode(StrEnum):
    cancel = "cancel"       # SSE 断连后取消运行
    continue_ = "continue"  # SSE 断连后继续运行
```

在 SSE 流的 `finally` 块中处理：

```python
finally:
    if record.status in (RunStatus.pending, RunStatus.running):
        if record.on_disconnect == DisconnectMode.cancel:
            await run_mgr.cancel(record.run_id)
```

- **cancel 模式**：SSE 断连后，如果 run 还在 running，调用 `cancel()` 中止
- **continue 模式**：SSE 断连后，Agent 继续在后台执行，只是事件没人消费了

**关键点**：关闭页面不一定停止 Agent——取决于创建 run 时传的 `on_disconnect` 参数。

#### (c) LangGraph 的 interrupt 中断（Human-in-the-loop）

这是 LangGraph 原生的中断机制，通过 `interrupt_before`/`interrupt_after` 节点设置：

1. Agent 在指定节点前/后暂停执行（如危险操作前等待人类确认）
2. 检查点状态由 LangGraph Checkpointer 持久化（通常是数据库）
3. Run 状态变为 `success`（因为 `astream` 正常结束了迭代）
4. 恢复时：发起新的 run，LangGraph 从检查点自动恢复

### 内存缓存的生命周期

| 组件 | 保留时长 | 清理方式 |
|------|----------|----------|
| **RunRecord**（内存） | 默认 5 分钟后清理 | `cleanup(run_id, delay=300)` |
| **StreamBridge 缓冲区** | 运行结束后 60 秒清理 | `bridge.cleanup(run_id, delay=60)` |
| **RunStore**（数据库） | 永久（如果配置了） | 数据库管理 |
| **LangGraph Checkpointer** | 永久 | 数据库管理（对话状态） |

### 进程重启后的恢复

- **如果配置了 RunStore**：run 的元数据持久化到数据库，进程重启后可通过 `RunManager.get()` 从数据库恢复查询（`_record_from_store()`），但恢复的是只读的历史记录，无法恢复 `asyncio.Task` 和 `abort_event`
- **如果没有 RunStore**：run 记录仅存内存，进程重启后丢失
- **LangGraph 检查点**（对话状态）是独立持久化的（SQLite/PostgreSQL），不受 RunStore 影响，进程重启后对话上下文仍然保留

### 总结

中断的执行**不是**一直缓存在内存中。内存中的 RunRecord 只是活跃 run 的运行时控制结构（task、abort_event 等不可序列化的对象），有明确的清理机制。持久化的数据分两层：

1. **Run 元数据**：通过可选的 RunStore 持久化到数据库
2. **对话状态**：通过 LangGraph Checkpointer 持久化到数据库（始终存在）

两者相互独立，确保即使内存被清理，用户仍可从断点恢复对话。
