# run_agent 与 StreamBridge、LangGraph 推流协作

从 `agent.astream()` 产生 chunk 到前端收到 SSE 帧的完整链路，以及多种流式模式的定义与转换。

---

## 整体链路

```
前端请求 stream_mode=["messages-tuple", "values"]
        │
        ▼
start_run() → normalize_stream_modes() → stream_modes=["messages-tuple", "values"]
        │
        ▼
asyncio.create_task(run_agent(..., stream_modes=...))
        │
        ├─ 1. 构建流式模式映射：requested_modes → lg_modes
        ├─ 2. agent.astream(graph_input, stream_mode=lg_modes)
        │     └─ LangGraph ReAct 循环产出 chunk
        ├─ 3. serialize(chunk, mode=mode)
        │     └─ 将 LangChain 对象转为 JSON 可序列化结构
        ├─ 4. bridge.publish(run_id, sse_event, serialized_data)
        │     └─ 推入 asyncio.Queue
        │
        ▼
sse_consumer: bridge.subscribe(run_id)
        │
        ├─ queue.get() → StreamEvent
        ├─ format_sse(event, data) → SSE 文本帧
        └─ yield → StreamingResponse → 前端
```

---

## 一、流式模式定义

### 前端请求的模式（requested_modes）

前端通过 `body.stream_mode` 指定想要的输出格式，支持字符串或字符串列表：

| 模式名 | 说明 | LangGraph 映射 |
|--------|------|----------------|
| `"values"` | 每步的完整状态快照 | `"values"` |
| `"messages-tuple"` | 每条消息的增量更新（含 metadata） | `"messages"` |
| `"updates"` | 每步的增量 diff | `"updates"` |
| `"events"` | LangGraph 事件流 | **不支持，跳过** |
| `"checkpoints"` | 检查点变化 | `"checkpoints"` |
| `"tasks"` | 任务执行事件 | `"tasks"` |
| `"debug"` | 调试信息 | `"debug"` |
| 未指定 / 空列表 | 默认 | `["values"]` |

### LangGraph 原生模式（lg_modes）

`_VALID_LG_MODES = {"values", "updates", "checkpoints", "tasks", "debug", "messages", "custom"}`

`run_agent` 第 6 步将前端模式翻译为 LangGraph 模式：

```python
lg_modes: list[str] = []
for m in requested_modes:
    if m == "messages-tuple":
        lg_modes.append("messages")      # 映射
    elif m == "events":
        continue                          # 不支持，跳过
    elif m in _VALID_LG_MODES:
        lg_modes.append(m)               # 透传
if not lg_modes:
    lg_modes = ["values"]                # 兜底默认
```

翻译后去重保持顺序，传给 `agent.astream(stream_mode=lg_modes)`。

---

## 二、run_agent 的执行步骤

### 步骤 1-5：准备阶段

```
1. run_manager.set_status(running)          ← 标记状态
2. bridge.publish("metadata", {run_id, thread_id})  ← 发布元数据
3. Runtime 注入 + agent_factory(config) → agent     ← 构建 Agent
4. agent.checkpointer / store 挂载
5. interrupt_before / interrupt_after 设置
```

### 步骤 6：构建 lg_modes

见上一节。关键：`requested_modes` 是 set（去重），`lg_modes` 是 list（保持顺序给 LangGraph）。

### 步骤 7：流式执行（核心）

根据 lg_modes 数量走两条路径：

**路径 A — 单模式、无子图**：

```python
single_mode = lg_modes[0]  # 如 "values" 或 "messages"
async for chunk in agent.astream(graph_input, config, stream_mode=single_mode):
    if record.abort_event.is_set():
        break
    sse_event = _lg_mode_to_sse_event(single_mode)   # 直接用模式名
    await bridge.publish(run_id, sse_event, serialize(chunk, mode=single_mode))
```

- LangGraph `astream` 直接产出原始 chunk（不是元组）
- 性能更好，是常见路径

**路径 B — 多模式或带子图**：

```python
async for item in agent.astream(graph_input, config, stream_mode=lg_modes, subgraphs=stream_subgraphs):
    if record.abort_event.is_set():
        break
    mode, chunk = _unpack_stream_item(item, lg_modes, stream_subgraphs)
    sse_event = _lg_mode_to_sse_event(mode)
    await bridge.publish(run_id, sse_event, serialize(chunk, mode=mode))
```

- LangGraph `astream` 产出元组 `(mode, chunk)` 或 `(namespace, mode, chunk)`
- `_unpack_stream_item()` 解包为 `(mode, chunk)`

### 步骤 8：最终状态

```python
if record.abort_event.is_set():
    if action == "rollback":
        set_status(error, "Rolled back")    # 回滚（Phase 2 完善）
    else:
        set_status(interrupted)             # 中断
else:
    set_status(success)                     # 成功
```

异常处理中会额外 `bridge.publish("error", {message, name})` 推送错误事件。

### finally：清理

```python
await bridge.publish_end(run_id)                        # 发送 END_SENTINEL
asyncio.create_task(bridge.cleanup(run_id, delay=60))   # 60s 后释放队列
```

---

## 三、序列化（serialization.py）

LangGraph 产出的 chunk 是 LangChain 对象（AIMessage、ToolMessage 等），不能直接 JSON 序列化。`serialize()` 按模式分别处理：

| 模式 | chunk 类型 | 序列化方式 |
|------|-----------|-----------|
| `"messages"` | `(message_chunk, metadata_dict)` | `serialize_messages_tuple()` → `[序列化chunk, metadata]` |
| `"values"` | 完整状态 dict | `serialize_channel_values()` → 移除 `__pregel_*` 和 `__interrupt__` 键，递归 `model_dump()` |
| 其他 | 任意 | `serialize_lc_object()` → 递归处理 dict/list/Pydantic/str |

序列化后得到纯 JSON 结构，传给 `bridge.publish()`。

---

## 四、Bridge 推流

```python
await bridge.publish(run_id, sse_event, serialized_data)
```

- `run_id`：找到对应的 `asyncio.Queue`
- `sse_event`：事件名（`"values"` / `"messages"` / `"updates"` / `"error"` / `"metadata"`）
- `serialized_data`：JSON 可序列化的载荷

Bridge 内部：`StreamEvent(id="{ts}-{seq}", event=sse_event, data=serialized_data)` → `queue.put(entry)`

---

## 五、sse_consumer 消费

```python
async def sse_consumer(bridge, record, request, run_mgr):
    async for entry in bridge.subscribe(record.run_id):
        if await request.is_disconnected():
            break
        if entry is HEARTBEAT_SENTINEL:
            yield ": heartbeat\n\n"
        elif entry is END_SENTINEL:
            yield format_sse("end", None)
            return
        else:
            yield format_sse(entry.event, entry.data, event_id=entry.id)
```

`format_sse()` 输出格式：

```
event: values
data: {"messages":[...],"title":"..."}
id: 1716000000000-0

```

匹配 LangGraph Platform 的 SSE 线协议，前端 `useStream` hook 按事件名分发处理。

---

## 六、完整时序

```
run_agent [后台 Task]                              sse_consumer [StreamingResponse]
  │                                                  │
  ├─ set_status(running)                             │
  ├─ publish("metadata", {run_id, thread_id})        │
  │                                                  ├─ subscribe → queue.get → format_sse("metadata")
  │                                                  │
  ├─ agent_factory(config) → agent                   │
  │                                                  │
  ├─ agent.astream(graph_input, stream_mode="messages")
  │   │                                              │
  │   ├─ LLM 调用 → AIMessage chunk                  │
  │   │  serialize(chunk, mode="messages")            │
  │   │  publish("messages", [serialized, metadata])  │
  │   │                                               ├─ queue.get → format_sse("messages")
  │   │                                               │  → 前端收到消息增量
  │   │                                               │
  │   ├─ 工具调用 → ToolMessage chunk                 │
  │   │  publish("messages", [serialized, metadata])  │
  │   │                                               ├─ queue.get → format_sse("messages")
  │   │                                               │  → 前端收到工具结果
  │   │                                               │
  │   ├─ ...ReAct 循环继续...                          │
  │   │                                               │
  │   │  （15s 无数据）                                 │
  │   │                                               ├─ timeout → ": heartbeat\n\n"
  │   │                                               │
  │   ├─ 用户取消 → abort_event.is_set()              │
  │   │  break                                        │
  │   │                                               │
  ├─ set_status(success/interrupted/error)            │
  │                                                  │
  ├─ finally:                                        │
  │   publish_end(run_id)                            │
  │   cleanup(delay=60)                              │
  │                                                  ├─ END_SENTINEL → format_sse("end") → return
  │                                                  │
  │                                                  ├─ finally: 断连检查
  │                                                  │  cancel 模式 → run_mgr.cancel()
```

---

## 七、取消与异常处理

| 场景 | run_agent 行为 | Bridge 行为 | sse_consumer 行为 |
|------|---------------|-------------|-------------------|
| 正常完成 | `set_status(success)` | `publish_end` → `cleanup(60)` | 收到 END → 关闭流 |
| 用户取消 | `abort_event.is_set()` → break | 同上 | 同上 |
| CancelledError | `set_status(interrupted)` 或 rollback | 同上 | 同上 |
| 执行异常 | `publish("error", ...)` + `set_status(error)` | 同上 | 收到 error 事件 → 收到 END → 关闭 |
| 客户端断连 | 不感知（继续跑或被 cancel） | 队列满则丢弃 | `is_disconnected()` → break → finally 中按策略 cancel/continue |

---

> 本文档：`run_agent` 是推流链路的编排中心——将前端请求的模式翻译为 LangGraph 原生模式，调用 `agent.astream()` 获取 chunk，经 `serialize()` 转为 JSON 后通过 `bridge.publish()` 推入队列，`sse_consumer` 从队列取出并格式化为 SSE 帧推给前端。单模式走快速路径（原始 chunk），多模式走元组路径（解包 mode+chunk）。取消、异常、断连各有独立的处理策略。
