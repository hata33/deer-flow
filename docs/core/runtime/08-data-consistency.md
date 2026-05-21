# 08 - 流式输出与持久化的数据一致性

## 问题

流式推流（StreamBridge → SSE）和事件持久化（RunJournal → RunEventStore → DB）走的是两条独立路径，二者的内容一致吗？

## 答案：内容一致，结构不同

一次 Run 中存在 **四条独立的数据路径**，它们记录的是同一执行过程的不同切面：

```
agent.astream(config, stream_mode="values")
  │
  ├─ [1] StreamBridge 推流 ──→ 前端实时渲染     ← 完整 ThreadState 快照
  ├─ [2] Checkpointer 自动写入 ──→ 状态恢复/续聊  ← 完整 ThreadState 快照（与 [1] 同源）
  ├─ [3] RunJournal 回调 ──→ RunEventStore ──→ run_events 表  ← 细粒度事件记录
  └─ [4] get_completion_data() ──→ runs 表    ← Token 聚合统计
```

| 路径 | 触发者 | 数据格式 | 存储位置 | 用途 |
|------|--------|----------|----------|------|
| StreamBridge | `bridge.publish()` 显式调用 | 完整 `ThreadState`（messages/artifacts/title...） | 不持久化 | 前端 SSE 渲染 |
| Checkpointer | LangGraph 自动写入 | 完整 `ThreadState`（与推流同源） | SQLite/Postgres/Memory | 状态恢复、历史回放 |
| RunJournal | LangChain 回调自动触发 | `{event_type, content: message.model_dump()}` | `run_events` 表 / JSONL | 事件审计、成本分析 |
| RunRow | `finally` 块显式调用 | `{total_tokens, last_ai_message(截断), ...}` | `runs` 表 | 运行列表、Token 账单 |

## 为什么内容一致

- **StreamBridge** 推送的 `chunk["messages"][-1]` 和 **RunJournal** `on_llm_end` 回调中的 `message.model_dump()` 是**同一条 AIMessage**——前者来自 `astream()` 产出的状态快照，后者来自 LangChain 框架在同一个 LLM 调用完成时触发的回调
- **Checkpointer** 写入的检查点与 **StreamBridge** 推送的 values 事件来自**同一个 `astream()` 输出**，只是目的不同
- **RunRow** 中的 `last_ai_message` 是从 RunJournal 累积的同一个 AI 消息中截取的（前 2000 字符）

## 触发时机的差异

```
agent.astream 每步产出 chunk
  ├─ [同步] bridge.publish(chunk)          ← 立即推流
  └─ [LangChain 内部同步] on_llm_end()    ← 回调 → _put() 缓冲
                       │
                       └─ [异步] _flush_sync() → RunEventStore.put_batch()
                                          
finally:
  ├─ [异步] journal.flush()                ← 刷新残留在缓冲区的
  └─ [异步] update_run_completion(...)      ← 写入 RunRow
```

推流是**实时、同步**的（每步产出即推送）；RunJournal 写入是**缓冲 + 批量异步**的（累计 20 条或 run 结束时刷新）。但**消息内容本身来自同一次 LLM 调用**，不存在数据不一致。
