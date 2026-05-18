# StreamBridge 发布订阅模式

解耦 agent 执行（生产者）和 SSE 推流（消费者）的异步桥接层。生产者和消费者在不同协程中运行，互不阻塞，通过 `run_id` 绑定一对队列。

---

## 文件结构

```
runtime/stream_bridge/
  ├─ base.py              ← StreamBridge 抽象基类 + StreamEvent / 哨兵定义
  ├─ memory.py            ← MemoryStreamBridge（asyncio.Queue 实现）
  ├─ async_provider.py    ← make_stream_bridge() 工厂函数
  └─ __init__.py          ← 统一导出

config/stream_bridge_config.py   ← 配置模型（type / queue_maxsize / redis_url）
app/gateway/deps.py              ← 全局单例创建 + per-request getter
app/gateway/services.py          ← sse_consumer 消费 bridge
```

---

## 抽象接口（base.py）

`StreamBridge` 定义 4 个抽象方法：

| 方法 | 角色 | 说明 |
|------|------|------|
| `publish(run_id, event, data)` | 生产者 | 将事件入队，30s 超时则丢弃 |
| `publish_end(run_id)` | 生产者 | 发送结束哨兵，通知消费者关闭 |
| `subscribe(run_id)` | 消费者 | 异步迭代器，yield 事件/心跳/结束 |
| `cleanup(run_id, delay)` | 清理 | 延迟释放队列和计数器 |

数据结构：

```python
StreamEvent(id="{timestamp_ms}-{seq}", event="values", data={...})
HEARTBEAT_SENTINEL  # 15s 无数据时发心跳保活
END_SENTINEL        # 结束信号
```

哨兵用 `is` 比较，是模块级单例。

---

## 内存实现（memory.py）

`MemoryStreamBridge` 核心数据结构：

```python
self._queues: dict[str, asyncio.Queue[StreamEvent]] = {}   # run_id → Queue(maxsize=256)
self._counters: dict[str, int] = {}                         # run_id → 事件序号
```

**publish**：按 `run_id` 找到（或创建）队列，构造 `StreamEvent` 后 `queue.put(entry, timeout=30s)`。队列满则丢弃事件并记警告日志，不阻塞 agent。

**subscribe**：`while True` 循环，`queue.get(timeout=heartbeat_interval)` 取事件。超时 yield 心跳，收到 `END_SENTINEL` 则 yield 后 return，正常事件直接 yield。

**cleanup**：`delay` 秒后从 `_queues` 和 `_counters` 中删除对应 `run_id` 的条目。

---

## 创建与注入链

```
app.py lifespan()
  └─ langgraph_runtime(app)                    [deps.py:20]
      ├─ make_stream_bridge()                  [async_provider.py:20]
      │   ├─ get_stream_bridge_config()        ← 读 config.yaml stream_bridge 段
      │   ├─ None 或 type="memory"
      │   │   └─ MemoryStreamBridge(queue_maxsize=256)
      │   └─ yield bridge + finally: close()
      └─ app.state.stream_bridge = bridge      ← 存为全局单例

每次请求：
  路由层 → get_stream_bridge(request)          [deps.py:44]
         → request.app.state.stream_bridge      ← 返回 MemoryStreamBridge 实例
```

`deps.py` 中 getter 的类型标注是抽象类 `StreamBridge`，实际返回 `MemoryStreamBridge`——依赖倒置，调用方只依赖接口。

---

## 配置（stream_bridge_config.py）

```yaml
# config.yaml
stream_bridge:
  type: memory          # memory（默认）| redis（Phase 2 未实现）
  queue_maxsize: 256    # 每个运行的最大缓冲事件数
```

未配置时回退到 `memory` + 默认参数。Redis 类型预留了 `redis_url` 字段，尚未实现。

`AppConfig.from_file()` 按 key 分发到 `load_stream_bridge_config_from_dict()`，模块内全局变量 `_stream_bridge_config` 独立缓存。

---

## 生产者：run_agent（worker.py）

```python
# 启动时发布 metadata
await bridge.publish(run_id, "metadata", {...})

# ReAct 循环中逐块推流
async for chunk in agent.astream(...):
    await bridge.publish(run_id, sse_event, serialize(chunk))

# 结束时
await bridge.publish_end(run_id)                            # 通知消费者关闭
asyncio.create_task(bridge.cleanup(run_id, delay=60))       # 60s 后清理队列
```

---

## 消费者：sse_consumer（services.py）

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

`format_sse()` 将事件格式化为 `event: ...\ndata: {json}\nid: ...\n\n`，匹配 LangGraph Platform 的 SSE 线协议，被前端 `useStream` hook 消费。

断连时 `finally` 块检查 `on_disconnect` 策略：`cancel` 模式下调用 `run_mgr.cancel()` 取消后台任务，`continue` 模式下让 agent 继续跑（事件丢弃）。

---

## 生产消费时序

```
run_agent [后台 Task]                          sse_consumer [StreamingResponse]
  │                                              │
  ├─ publish("metadata", {...})                  │
  │  → queue.put(StreamEvent)                    │
  │                                              ├─ queue.get() → format_sse → yield
  │                                              │
  ├─ async for chunk in agent.astream():         │
  │   publish("values", serialize(chunk))        │
  │                                              ├─ queue.get() → format_sse → yield
  │                                              │
  │  （15s 无数据）                               │
  │                                              ├─ queue.get() timeout → ": heartbeat\n\n"
  │                                              │
  ├─ finally:                                    │
  │   publish_end(run_id)                        │
  │   → queue.put(END_SENTINEL)                  │
  │                                              ├─ queue.get() → END → format_sse("end") → return
  │   cleanup(delay=60)                          │
  │   → 60s 后 del queue                         │
```

---

## 为什么用抽象类

| 原因 | 说明 |
|------|------|
| 可替换 | 工厂 `make_stream_bridge()` 预留了 Redis 实现，改配置即可切换 |
| 依赖倒置 | worker.py 和 services.py 只依赖 `StreamBridge` 接口，不关心底层是 Queue 还是 Redis |
| 测试友好 | 可以 mock StreamBridge 做单元测试，不需要真实 asyncio.Queue |
| 配置驱动 | config.yaml 的 `stream_bridge.type` 切换实现，业务代码无需改动 |

---

> 本文档：StreamBridge 通过抽象基类 + 工厂模式实现发布订阅，当前内存实现基于 `dict[str, asyncio.Queue]` 按 `run_id` 分发。生产者（run_agent）每步 publish，消费者（sse_consumer）subscribe 迭代取事件并格式化为 SSE。队列满丢弃不阻塞，15s 无数据发心跳保活，结束后 60s 延迟清理。Redis 实现为预留接口，尚未实现。
