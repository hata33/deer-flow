# Runtime 设计决策

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

## 核心决策清单

| # | 决策 | 解决的问题 | 权衡 |
|---|------|-----------|------|
| 1 | StreamBridge 抽象基类 + Memory 实现 | 生产/测试后端可替换 | 抽象层引入间接调用开销 |
| 2 | RunManager.create_or_reject() 原子操作 | TOCTOU 竞争：has_inflight + create 间隙 | 持锁范围增大，吞吐略降 |
| 3 | Worker 在独立 asyncio.Task 中运行 | 不阻塞 SSE 端点响应 | 调试栈更深，异常传播需显式处理 |
| 4 | 心跳间隔 15s | 反向代理/CDN 连接超时断开 | 空闲时每 15s 一次无用传输 |
| 5 | Last-Event-ID 重连支持 | 网络抖动导致 SSE 断开后事件丢失 | 需要内存保留事件窗口 |

---

## 决策 1：StreamBridge 抽象基类 + MemoryStreamBridge 实现

### 动机

SSE 端点（消费者）和 agent 工作线程（生产者）运行在同一个 asyncio 事件循环中，但生命周期完全不同步。需要一个解耦层将两者隔开。

### 设计选择

`StreamBridge` 被定义为 `abc.ABC`，仅声明四个核心操作：`publish`、`publish_end`、`subscribe`、`cleanup`。默认提供 `MemoryStreamBridge` 实现，基于进程内事件日志 + `asyncio.Condition` 通知机制。

### 为什么不用 asyncio.Queue

`Queue` 是一次性的——消费者 `get()` 后事件消失，无法支持：
- 多个 SSE 客户端同时订阅同一 run
- `Last-Event-ID` 重连时回放历史事件

`_RunStream` 使用 `list[StreamEvent]` 保留事件窗口，配合 `condition.notify_all()` 唤醒所有等待中的订阅者。

### 权衡

- 事件保留在内存中（`_maxsize=256`），超过上限后丢弃最旧事件。迟到太多的订阅者只能从最早保留事件处开始。
- 未来替换为 Redis Streams 等外部后端时，只需实现新的 `StreamBridge` 子类，上层代码无需修改。

---

## 决策 2：RunManager.create_or_reject() 原子多任务策略

### 动机

在并发请求下，"检查是否有活跃运行" + "创建新运行"之间存在经典的 TOCTOU 竞争窗口：

```
请求A: has_inflight(thread_1) → False
请求B: has_inflight(thread_1) → False   ← 同时看到无活跃运行
请求A: create(thread_1)                  ← 创建成功
请求B: create(thread_1)                  ← 也创建成功，现在同一线程有两个活跃运行
```

### 设计选择

`create_or_reject()` 在单个 `async with self._lock` 块内完成检查和插入，消除竞争窗口。同时将多任务策略（reject / interrupt / rollback）集成到同一原子操作中，使"取消旧运行 + 创建新运行"也具备原子性。

### 三种策略的行为

| 策略 | 有活跃运行时的行为 |
|------|-------------------|
| `reject` | 抛出 `ConflictError`，新运行不创建 |
| `interrupt` | 取消活跃运行（保留检查点），然后创建新运行 |
| `rollback` | 取消活跃运行（回滚到运行前检查点），然后创建新运行 |

### 权衡

- 持锁时间比单独 `create()` 稍长（需要遍历所有 inflight 运行），但实践中 inflight 数量极少（通常 0-1 个）。
- 持久化（`_persist_status`）在锁外执行，不阻塞其他操作。

---

## 决策 3：Worker 在独立 asyncio.Task 中运行

### 动机

`run_agent()` 函数执行时间可能长达数分钟（LLM 推理、工具调用等）。如果直接在 SSE 端点的请求处理协程中运行，会阻塞整个 HTTP 请求栈，导致：
- 无法返回 `202 Accepted`（"已创建，正在处理"）
- 无法在等待期间处理取消请求
- 其他 HTTP 请求可能被饿死

### 设计选择

`run_agent()` 被 `asyncio.create_task()` 包装为后台任务。SSE 端点立即返回，通过 `StreamBridge.subscribe()` 异步消费事件。

```
SSE 端点 ──create_task──> [Worker Task: run_agent()]
    │                              │
    │◄──subscribe──StreamBridge──publish─┘
```

### 权衡

- 异常不会自动传播到 SSE 端点，需要通过 `bridge.publish(run_id, "error", ...)` 显式传递错误事件，然后在 `finally` 块中 `publish_end`。
- 调试时需要查看 `record.task` 的异常信息，而非 HTTP 请求栈。

---

## 决策 4：心跳间隔 15 秒

### 动机

SSE 连接经过 Nginx 反向代理时，Nginx 默认的 `proxy_read_timeout` 为 60 秒。如果代理在此时间内没有收到任何数据，会单方面断开连接。

更关键的是，某些 CDN 和企业防火墙会在更短时间内关闭"空闲"TCP 连接。15 秒是一个经过验证的保守值，足以在大多数网络环境中保持连接活跃。

### 设计选择

`subscribe()` 方法在 `heartbeat_interval`（默认 15.0s）内没有新事件时，产生 `HEARTBEAT_SENTINEL`。SSE 端点识别此哨兵并写入 `:\n\n` 注释行（SSE 标准心跳格式），不携带任何业务数据。

### 权衡

- 空闲连接每 15 秒产生一次网络传输，在大量并发 SSE 连接时会产生累积开销。
- 如果确认部署环境没有中间代理超时限制，可以增大此值减少无用传输。

---

## 决策 5：Last-Event-ID 重连支持

### 动机

SSE 规范定义了 `Last-Event-ID` 请求头，客户端在断线重连时携带此头部，告知服务端最后成功接收的事件 ID。如果不支持此机制，断线期间的事件将永久丢失。

### 设计选择

每个 `StreamEvent` 携带单调递增的 ID（格式 `{timestamp_ms}-{sequence}`）。`MemoryStreamBridge._resolve_start_offset()` 根据 `Last-Event-ID` 在保留事件窗口中定位起始偏移，从该位置开始重放。

```python
def _resolve_start_offset(self, stream, last_event_id):
    if last_event_id is None:
        return stream.start_offset
    for index, entry in enumerate(stream.events):
        if entry.id == last_event_id:
            return stream.start_offset + index + 1  # +1: 从下一个事件开始
    # 未找到：从最早保留事件开始
    return stream.start_offset
```

### 权衡

- 事件窗口有限（默认 256 个）。如果客户端断线时间过长导致事件被淘汰，只能从最早保留事件开始重放，中间事件丢失。
- 每个事件携带 ID 字符串增加了 SSE 帧的体积（通常可忽略）。
