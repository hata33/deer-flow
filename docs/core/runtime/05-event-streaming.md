# 05 - 事件流系统：StreamBridge

## 解决的问题

Agent 在后台 asyncio Task 中执行，前端通过 HTTP SSE（Server-Sent Events）连接接收实时更新。这两个过程运行在不同的异步上下文中：

- **生产者**（run_agent）：执行 Agent 图，产生状态更新、消息片段、工具调用结果
- **消费者**（Gateway SSE 端点）：持有 HTTP 连接，将事件格式化为 SSE 帧发送给浏览器

如果没有中间管道，生产者必须直接操作 HTTP 响应，导致两个问题：
1. 一个客户端断线就会中断 Agent 执行
2. 无法支持多个客户端同时订阅同一个运行

StreamBridge 作为中间管道解决了这些问题。

## 核心设计

StreamBridge 的接口只有四个方法：

| 方法 | 调用者 | 作用 |
|------|--------|------|
| `publish(run_id, event, data)` | run_agent（生产者） | 将一个事件放入 run_id 的管道 |
| `publish_end(run_id)` | run_agent（生产者） | 通知管道"这个运行不会再有更多事件了" |
| `subscribe(run_id)` | SSE 端点（消费者） | 返回一个异步迭代器，逐个产出事件 |
| `cleanup(run_id, delay)` | run_agent 收尾 | 延迟清理 run_id 的缓冲区 |

## MemoryStreamBridge 的内部结构

### 每个 run_id 维护一个独立的流状态（_RunStream）

```
_RunStream:
  - events: list[StreamEvent]    # 事件缓冲区（有界，默认 256 条）
  - condition: asyncio.Condition  # 用于通知消费者"有新事件了"
  - ended: bool                   # 生产者是否已经结束
  - start_offset: int             # 因为缓冲区溢出被裁剪掉的事件数
```

### 事件 ID 的设计

每个事件有一个格式为 `{timestamp_ms}-{sequence}` 的 ID，例如 `1700000000000-0`。这个 ID 有两个用途：
1. SSE 协议的 `id:` 字段，客户端用它跟踪已接收的最后事件
2. 断线重连时，客户端通过 `Last-Event-ID` 告诉服务端从哪里恢复

### 缓冲区溢出处理

每个 run_id 的缓冲区最多保留 256 条事件。超过时，最早的事件被丢弃，start_offset 递增。这是为了防止一个长时间运行的 Agent 占用过多内存。

如果消费者落后太多（请求的 offset 已经被裁剪），会从当前缓冲区最早的事件开始发送，并在日志中记录警告。

## 生产者侧流程（run_agent 如何发布事件）

```
run_agent 每次收到 LangGraph 的 astream chunk:
  │
  ├── 序列化 chunk 为 JSON（serialize 函数）
  │
  ├── 确定事件名称（values、messages、updates、error、metadata 等）
  │
  └── 调用 bridge.publish(run_id, event_name, serialized_data)
       │
       ├── 获取或创建该 run_id 的 _RunStream
       ├── 分配递增的事件 ID
       ├── 将事件追加到 events 列表
       ├── 如果超过 maxsize，裁剪旧事件
       └── 通过 condition.notify_all() 唤醒等待的消费者

运行结束时:
  └── 调用 bridge.publish_end(run_id)
       └── 设置 ended=True，唤醒所有等待的消费者
```

## 消费者侧流程（SSE 端点如何消费事件）

```
Gateway SSE 端点调用 bridge.subscribe(run_id, last_event_id=...):
  │
  ├── 根据 last_event_id 计算起始 offset
  │    （如果没有 last_event_id，从当前缓冲区末尾开始，即只接收新事件）
  │
  └── 进入循环:
       │
       ├── 检查 local_index 是否在缓冲区范围内
       │    ├── 是 → 取出事件，yield 给调用者
       │    └── 否 → 检查是否已结束
       │         ├── 已结束 → yield END_SENTINEL，退出循环
       │         └── 未结束 → 等待 condition（最多 heartbeat_interval 秒）
       │              ├── 超时 → yield HEARTBEAT_SENTINEL
       │              └── 被唤醒 → 回到循环顶部继续检查
       │
       └── END_SENTINEL 被产出后，异步迭代器结束
```

## 心跳机制

SSE 连接通常经过反向代理（如 Nginx）。如果没有数据传输，代理会在超时后关闭连接（Nginx 默认 60 秒）。

StreamBridge 在 `heartbeat_interval`（默认 15 秒）内没有新事件时，自动发送一个心跳哨兵事件（`__heartbeat__`）。Gateway 的 SSE 消费者检测到心跳事件后发送 SSE 注释帧（`: heartbeat\n\n`），这足以保持连接活跃。

## 断线重连

客户端断线后重连时，通过 `Last-Event-ID` 请求头告诉服务端它最后收到的事件 ID。StreamBridge 的处理：

1. 遍历缓冲区查找匹配的事件 ID
2. 从该 ID 的下一个事件开始发送
3. 如果 ID 已经不在缓冲区中（因为缓冲区溢出），从缓冲区最早的事件开始发送

这确保了客户端在网络不稳定的情况下仍然能获取所有事件，最多丢失溢出缓冲区的旧事件。

## 延迟清理

run_agent 在 finally 块中调用 `bridge.cleanup(run_id, delay=60)`。60 秒延迟是为了：
- 给迟到的订阅者时间连接并消费剩余事件
- 给正在进行重连的客户端时间完成恢复
- 避免在 SSE 响应还在传输时就销毁缓冲区

## SSE 事件格式

Gateway 将 StreamEvent 格式化为标准 SSE 帧发送给前端：

```
event: metadata
data: {"run_id":"xxx","thread_id":"yyy"}
id: 1700000000000-0

event: messages
data: {"messages":[...]}
id: 1700000000000-1

event: values
data: {"messages":[...],"title":"..."}
id: 1700000000000-2

event: end
data: {}
id: 1700000000000-3
```

前端使用 LangGraph SDK 的 `useStream` React Hook 解析这些事件，实现了与 LangGraph Platform 兼容的客户端协议。
