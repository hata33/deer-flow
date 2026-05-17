StreamBridge 发布订阅模式——从抽象接口到内存实现的完整注入链与工作原理

---

## 一、你的疑问

代码中到处用的是 `StreamBridge`（抽象基类），但 `base.py` 里的方法全是 `@abc.abstractmethod` 没有实现。实际的实现在哪？怎么注入的？

答案是：**工厂模式 + 依赖倒置**。代码只依赖抽象类型 `StreamBridge`，运行时由工厂 `make_stream_bridge()` 创建具体实现 `MemoryStreamBridge`，存到 `app.state` 上。后续所有代码通过 getter 获取时，类型标注写的是抽象类，但拿到的是具体实例

---

## 二、文件结构

```
runtime/stream_bridge/
  ├─ base.py              ← StreamBridge 抽象基类（4 个抽象方法）
  ├─ memory.py            ← MemoryStreamBridge 具体实现（asyncio.Queue）
  ├─ async_provider.py    ← make_stream_bridge() 工厂函数
  └─ __init__.py          ← 统一导出

app/gateway/
  ├─ deps.py              ← langgraph_runtime() 创建全局单例 + getter
  └─ services.py          ← sse_consumer() 消费 bridge
```

---

## 三、注入链（从 app 启动到使用）

```
app.py 启动
  │
  └─ lifespan() → langgraph_runtime(app)         [deps.py:20]
      │
      ├─ from deerflow.runtime import make_stream_bridge
      │
      ├─ make_stream_bridge()                     [async_provider.py:20]
      │   │
      │   ├─ get_stream_bridge_config()           ← 读 config.yaml
      │   ├─ config 为 None 或 type="memory"
      │   │   │
      │   │   └─ MemoryStreamBridge(queue_maxsize=256)  ← ★ 这里创建了具体实现
      │   │
      │   └─ yield bridge  ← 返回的是 MemoryStreamBridge 实例
      │
      └─ app.state.stream_bridge = bridge         ← 存到 app.state 上
          （类型是 StreamBridge，实际对象是 MemoryStreamBridge）

...每次 HTTP 请求...

路由层 thread_runs.py
  │
  ├─ bridge = get_stream_bridge(request)          [deps.py:44]
  │   └─ return request.app.state.stream_bridge   ← 返回存好的 MemoryStreamBridge
  │
  └─ start_run() → run_agent(bridge, ...)
      │
      ├─ worker.py: bridge.publish(...)            ← 调用 MemoryStreamBridge.publish()
      ├─ worker.py: bridge.publish_end(...)        ← 调用 MemoryStreamBridge.publish_end()
      └─ worker.py: bridge.cleanup(...)            ← 调用 MemoryStreamBridge.cleanup()

sse_consumer(bridge, record, ...)                  [services.py:266]
  └─ async for entry in bridge.subscribe(run_id)   ← 调用 MemoryStreamBridge.subscribe()
```

### 关键代码对照

**创建**（async_provider.py:28-33）：

```python
if config is None or config.type == "memory":
    bridge = MemoryStreamBridge(queue_maxsize=maxsize)  # ★ 具体实现
    yield bridge
```

**存储**（deps.py:32）：

```python
app.state.stream_bridge = await stack.enter_async_context(make_stream_bridge())
```

**获取**（deps.py:44-49）：

```python
def get_stream_bridge(request: Request) -> StreamBridge:  # 类型标注是抽象类
    bridge = getattr(request.app.state, "stream_bridge", None)
    return bridge  # ★ 实际返回的是 MemoryStreamBridge 实例
```

---

## 四、MemoryStreamBridge 实现（base.py 中每个抽象方法的具体逻辑）

### 4.1 数据结构

```python
class MemoryStreamBridge(StreamBridge):
    def __init__(self, *, queue_maxsize=256):
        self._queues: dict[str, asyncio.Queue[StreamEvent]] = {}  # run_id → Queue
        self._counters: dict[str, int] = {}                        # run_id → 事件计数器
```

每个 run_id 独立一个 `asyncio.Queue`，互不影响。队列容量默认 256

### 4.2 publish() — 生产者写入

```python
async def publish(self, run_id, event, data):
    queue = self._get_or_create_queue(run_id)       # 按需创建队列
    entry = StreamEvent(id=self._next_id(run_id), event=event, data=data)
    try:
        await asyncio.wait_for(queue.put(entry), timeout=30.0)  # 30s 超时
    except TimeoutError:
        logger.warning("queue full — dropping event")            # 满了就丢弃
```

调用者：`worker.py` 的 `run_agent()`，在 agent 每步执行后调用

### 4.3 subscribe() — 消费者读取

```python
async def subscribe(self, run_id, *, last_event_id=None, heartbeat_interval=15.0):
    queue = self._get_or_create_queue(run_id)
    while True:
        try:
            entry = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
        except TimeoutError:
            yield HEARTBEAT_SENTINEL    # 15s 无数据 → 发心跳
            continue
        if entry is END_SENTINEL:
            yield END_SENTINEL          # 结束信号 → 退出
            return
        yield entry                     # 正常事件 → yield 给消费者
```

调用者：`services.py` 的 `sse_consumer()`

### 4.4 publish_end() — 发送结束哨兵

```python
async def publish_end(self, run_id):
    queue = self._get_or_create_queue(run_id)
    await asyncio.wait_for(queue.put(END_SENTINEL), timeout=30.0)
```

调用者：`worker.py` 的 `finally` 块

### 4.5 cleanup() — 延迟释放资源

```python
async def cleanup(self, run_id, *, delay=0):
    if delay > 0:
        await asyncio.sleep(delay)      # 延迟 60s
    self._queues.pop(run_id, None)      # 删除队列
    self._counters.pop(run_id, None)    # 删除计数器
```

调用者：`worker.py` 的 `finally` 块，`asyncio.create_task(bridge.cleanup(run_id, delay=60))`

---

## 五、StreamEvent 数据结构

```python
@dataclass(frozen=True)
class StreamEvent:
    id: str       # 单调递增，格式 "{timestamp_ms}-{seq}"，用于 SSE id 字段
    event: str    # SSE 事件名：metadata / values / updates / error / __end__
    data: Any     # JSON 可序列化载荷

HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)
END_SENTINEL       = StreamEvent(id="", event="__end__", data=None)
```

哨兵对象用 `is` 比较（不是 `==`），因为它们是模块级单例

---

## 六、生产者与消费者的协作时序

```
run_agent() [后台 asyncio.Task]              sse_consumer() [StreamingResponse]
  │                                              │
  ├─ bridge.publish("metadata", {...})           │
  │  → queue.put(StreamEvent)                    │
  │                                              ├─ queue.get() → StreamEvent
  │                                              │  → format_sse("metadata", data)
  │                                              │  → yield SSE frame
  │                                              │
  ├─ async for chunk in agent.astream():         │
  │   bridge.publish("values", serialize(chunk)) │
  │   bridge.publish("values", ...)              │
  │                                              ├─ queue.get() → StreamEvent
  │                                              │  → format_sse("values", data)
  │                                              │  → yield SSE frame
  │                                              │
  │   ...（每步推流）                              │
  │                                              │
  │  （如果 15s 无数据）                            │
  │                                              ├─ queue.get() timeout
  │                                              │  → yield HEARTBEAT_SENTINEL
  │                                              │  → yield ": heartbeat\n\n"
  │                                              │
  ├─ finally:                                    │
  │   bridge.publish_end(run_id)                  │
  │   → queue.put(END_SENTINEL)                  │
  │                                              ├─ queue.get() → END_SENTINEL
  │   bridge.cleanup(delay=60)                    │  → yield format_sse("end", None)
  │   → 60s 后 del queue                          │  → return（关闭流）
```

**关键点**：
- 生产者和消费者在不同协程中，通过 `asyncio.Queue` 解耦
- agent 执行快 → queue 堆积 → 消费者按自己节奏取
- agent 执行慢 → 消费者等待 → 15s 无数据发心跳保活
- 队列满（256）→ 生产者等 30s → 仍满则丢弃事件（不阻塞 agent）
- 断连时消费者 finally 中检查 `on_disconnect` 决定是否 cancel 后台任务

---

## 七、为什么用抽象类而不是直接用 MemoryStreamBridge

| 原因 | 说明 |
|------|------|
| **可替换** | `async_provider.py` 中预留了 Redis 实现（Phase 2），只需改工厂返回不同实现 |
| **依赖倒置** | worker.py 和 services.py 只依赖 `StreamBridge` 接口，不关心具体实现 |
| **测试友好** | 可以 mock StreamBridge 进行单元测试，不需要真实的 asyncio.Queue |
| **配置驱动** | 通过 config.yaml 的 `stream_bridge.type` 切换实现，无需改业务代码 |

工厂 `make_stream_bridge()` 根据配置决定创建哪个实现，调用方只看到 `StreamBridge` 类型

---

> 本文档：StreamBridge 是发布订阅模式的抽象接口，`base.py` 定义 4 个抽象方法，`memory.py` 的 `MemoryStreamBridge` 用 `asyncio.Queue` 实现。工厂 `make_stream_bridge()` 在 app 启动时创建实例存入 `app.state`，getter `get_stream_bridge()` 返回时类型标注为抽象类但实际是具体实现。生产者（worker.py）每步 publish，消费者（sse_consumer）subscribe 迭代取事件，两者通过队列解耦
