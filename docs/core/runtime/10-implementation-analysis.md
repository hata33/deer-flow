# Runtime 实现分析

> 本文档基于源码逐层拆解实现细节。回答"代码怎么写的、为什么这么写"。

## 分层总览

```
runtime/
├── runs/
│   ├── schemas.py          # RunStatus, DisconnectMode 枚举
│   ├── manager.py          # RunManager + RunRecord
│   ├── worker.py           # run_agent() 后台执行入口
│   └── store/
│       ├── base.py         # RunStore 抽象接口
│       └── memory.py       # MemoryRunStore
├── stream_bridge/
│   ├── base.py             # StreamBridge ABC + StreamEvent
│   └── memory.py           # MemoryStreamBridge
├── events/store/           # 事件持久化
└── journal.py              # RunJournal 生命周期追踪
```

---

## 1. RunManager 生命周期

### 状态机

`RunRecord.status` 遵循以下状态转换：

```
pending ──► running ──► success
   │           ├──► error
   │           ├──► interrupted（用户取消）
   │           └──► error（rollback 取消）
   └──► interrupted（create_or_reject 策略取消）
```

### RunRecord 双层信息

`RunRecord` 是 `@dataclass`，同时承载两类信息：

| 类别 | 字段 | 说明 |
|------|------|------|
| 可序列化元数据 | `run_id`, `thread_id`, `status`, `model_name` 等 | 持久化到 RunStore |
| 运行时控制 | `task`, `abort_event`, `abort_action` | 仅存在于内存，不持久化 |

`store_only=True` 标记的记录从数据库恢复，无运行时控制字段，不支持取消操作。

### 双层存储

`RunManager` 同时维护内存注册表 `self._runs: dict[str, RunRecord]` 和可选持久化后端 `self._store: RunStore | None`。

`get()` 方法的查找优先级：内存 -> 持久化 -> 内存（二次确认）。二次确认防止并发 `create()` 在 store 查询期间插入内存记录。

---

## 2. Worker 流程

### run_agent() 执行路径

```
run_agent()
├── 1. set_status(running)
├── 2. 捕获 pre_run_checkpoint（用于 rollback）
├── 3. bridge.publish("metadata", {run_id, thread_id})
├── 4. 构建 agent（agent_factory + Runtime 注入 + RunJournal callback）
├── 5. agent.astream(graph_input, stream_mode=lg_modes)
│      ├── 单模式: async for chunk -> bridge.publish(sse_event, chunk)
│      └── 多模式: async for (mode, chunk) -> bridge.publish(mode, chunk)
├── 6. set_status(success | interrupted | error)
├── 7. journal.flush() + update_run_completion()
└── 8. bridge.publish_end() + cleanup(delay=60)
```

### 检查点回滚

`run_agent()` 启动时捕获 `pre_run_checkpoint_id` 和 `pre_run_snapshot`。rollback 时 `_rollback_to_pre_run_checkpoint()` 创建新 checkpoint ID 恢复快照，避免 ID 冲突，同时恢复 `pending_writes`。

### 上下文注入

`_build_runtime_context()` 构建运行时上下文字典，通过 `_install_runtime_context()` 注入到 LangGraph 的 `config["context"]` 和 `config["configurable"]["__pregel_runtime"]`，使 middleware 和工具可通过 `ToolRuntime.context` 访问 `thread_id`、`run_id` 和 `app_config`。

---

## 3. StreamBridge 发布/订阅模式

### 核心数据结构

`_RunStream` 包含：`events: list[StreamEvent]` 有界缓冲区、`condition: asyncio.Condition` 通知机制、`ended: bool` 结束标记、`start_offset: int` 缓冲区起始偏移。

### 生产者路径

`publish()` 调用 `_next_id()` 生成单调递增 ID（`{timestamp_ms}-{sequence}`），append 到 events 列表后 `condition.notify_all()` 唤醒所有订阅者。溢出时 `del events[:overflow]` 截断旧事件，同时 `start_offset += overflow`。

### 消费者路径

`subscribe()` 通过 `_resolve_start_offset()` 定位起始偏移，然后进入 `wait_for(condition, timeout=heartbeat_interval)` 循环。超时产生 `HEARTBEAT_SENTINEL`；`ended` 标记产生 `END_SENTINEL`。订阅者落后缓冲区时自动对齐到 `start_offset`。

### 偏移量管理

逻辑偏移与本地索引的映射：`local_index = logical_offset - start_offset`。溢出截断后逻辑偏移保持单调递增，`Last-Event-ID` 重连时通过遍历 events 列表定位恢复点。

---

## 4. 事件追踪与持久化

`RunJournal` 作为 LangChain callback handler 注入到 `config["callbacks"]`，拦截 `on_llm_end`（token 用量）和 `on_chain_start/end`（生命周期）。运行结束时 `flush()` 批量写入事件，`get_completion_data()` 一次性更新 token 统计和便利字段。

`RunStore` 定义运行元数据 CRUD 抽象，关键方法包括 `put`、`get`（含 user_id 过滤）、`list_by_thread`（降序分页）、`update_run_completion`（token+便利字段）、`aggregate_tokens_by_thread`（按模型聚合）。

所有 `_persist_*` 方法采用 best-effort 策略：异常仅记录日志，不向上传播，确保持久化失败不中断运行。`cleanup(delay=60)` 在 `publish_end()` 后延迟清理事件缓冲，给迟到订阅者留出耗尽时间。
