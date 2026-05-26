# Q&A 12: LangGraph 的推流和存储

> LangGraph 执行过程中的推流和持久化存储，是否全部通过 callback 机制处理？

---

## 答案：不是

推流和持久化通过**三个独立的机制**实现，而非全部通过 callback：

| 机制 | 职责 | 实现方式 |
|------|------|---------|
| **StreamBridge** | 事件传输（推流到客户端） | 发布/订阅模式 |
| **RunJournal** | 事件记录（token 用量、调试追踪） | LangChain Callback |
| **Checkpointer** | 状态持久化（断点恢复） | LangGraph 内置 |

---

## 一、StreamBridge — 事件传输

### 架构

```
Agent.astream()
    ↓ 产出事件
Worker (run_agent)
    ↓ bridge.publish(run_id, event, data)
StreamBridge
    ↓ 异步队列
SSE Consumer (sse_consumer)
    ↓ format_sse()
HTTP SSE 响应 → 前端
```

### 核心接口

```python
class StreamBridge(ABC):
    @abstractmethod
    async def publish(self, run_id: str, event: str, data: Any) -> None:
        """发布一个 SSE 事件"""

    @abstractmethod
    async def subscribe(self, run_id: str, last_event_id: str | None = None) -> AsyncIterator[StreamEvent]:
        """订阅一个 run 的事件流"""

    @abstractmethod
    async def publish_end(self, run_id: str) -> None:
        """发送结束信号"""
```

### 内存实现（MemoryStreamBridge）

```python
class MemoryStreamBridge(StreamBridge):
    # 每个 run 保留最近 256 个事件
    _max_events_per_run: int = 256

    # 事件 ID 格式：{timestamp}-{sequence}
    # 支持通过 Last-Event-ID 重连
```

### 推流的工作方式

```python
# runtime/runs/worker.py
async for chunk in agent.astream(
    graph_input,
    config=runnable_config,
    stream_mode=["values", "messages", "updates", "custom"],
):
    sse_event = _lg_mode_to_sse_event(mode)
    await bridge.publish(run_id, sse_event, serialize(chunk))
```

**关键**: 推流是通过 `agent.astream()` 的 async generator 实现的，**不是通过 callback**。

---

## 二、RunJournal — 事件记录

### 这是唯一使用 Callback 的机制

`RunJournal` 实现了 LangChain 的 `BaseCallbackHandler` 接口：

```python
class RunJournal(BaseCallbackHandler):
    def on_chain_start(self, ...): ...    # 记录链开始
    def on_chain_end(self, ...): ...      # 记录链结束
    def on_chat_model_start(self, ...): ...  # LLM 调用开始
    def on_llm_end(self, ...): ...       # LLM 调用结束（捕获 token 用量）
    def on_tool_start(self, ...): ...    # 工具调用开始
    def on_tool_end(self, ...): ...      # 工具调用结束
```

### 用途

RunJournal **只用于记录**，不影响推流或持久化：

| 用途 | 实现方式 |
|------|---------|
| Token 用量统计 | `on_llm_end` 捕获 `usage_metadata`，按 caller 分类累积 |
| 调试追踪 | 记录每次链/LLM/工具调用的开始和结束 |
| 子代理用量聚合 | 通过 tags 识别 subagent caller，合并到父级 |

### 注入方式

```python
# worker.py
journal = RunJournal(run_id=record.run_id)
config.setdefault("callbacks", []).append(journal)
```

RunJournal 作为 callback 注入到 LangGraph 的运行配置中。

---

## 三、Checkpointer — 状态持久化

### 这是 LangGraph 的内置机制

检查点持久化**完全由 LangGraph 框架处理**，不通过 callback：

```python
# 在创建 Agent 时注入
agent.checkpointer = checkpointer

# 运行时自动保存
# LangGraph 在每个节点执行后自动调用 checkpointer.put()
```

### 支持的后端

| 后端 | 实现 | 持久性 |
|------|------|--------|
| `MemorySaver` | 进程内 dict | 非持久（重启丢失） |
| `SqliteSaver` | SQLite | 持久 |
| `PostgresSaver` | PostgreSQL | 持久 |

### 检查点内容

```
AgentState:
    ├── messages: [...]           ← 所有对话消息
    ├── title: "..."              ← 线程标题
    ├── artifacts: [...]          ← 生成的文件路径
    ├── todos: [...]              ← 待办任务列表
    └── ...其他状态字段
```

每个 LangGraph 节点执行后，完整状态被保存到检查点。如果运行中断，LangGraph 可以从最后一个检查点恢复。

---

## 四、三者的解耦

```
                    ┌───────────────┐
                    │  agent.astream │ ← LangGraph 执行引擎
                    └───────┬───────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
              ▼             ▼             ▼
    ┌─────────────┐ ┌────────────┐ ┌─────────────┐
    │ StreamBridge │ │ RunJournal  │ │ Checkpointer│
    │ (推流传输)   │ │ (事件记录)  │ │ (状态持久化) │
    └─────────────┘ └────────────┘ └─────────────┘
         │               │               │
         ▼               ▼               ▼
    SSE 客户端      Token 统计       SQLite/PG
    (实时渲染)      (用量追踪)       (断点恢复)
```

### 各自的独立性

| 维度 | StreamBridge | RunJournal | Checkpointer |
|------|-------------|------------|-------------|
| 可以关闭吗 | 不能（核心功能） | 可以（调试功能） | 可以（但丢失恢复能力） |
| 互相依赖吗 | 不依赖 | 不依赖 | 不依赖 |
| 实现方式 | 自定义发布/订阅 | LangChain Callback | LangGraph 内置 |
| 数据流向 | Worker → SSE | LLM → Journal | LangGraph → DB |

---

## 五、推流的两种路径

### 路径 A：Gateway（HTTP SSE）

```
Frontend → POST /runs/stream → Worker (async Task)
    → agent.astream() → StreamBridge.publish()
    → sse_consumer() → SSE 帧格式化 → HTTP 响应
```

### 路径 B：DeerFlowClient（同步进程内）

```python
# client.py
for chunk in agent.stream(graph_input, config=config, stream_mode="values"):
    yield chunk  # 直接 yield，无需序列化
```

**为什么两条路径**:
- Gateway 路径：HTTP 客户端需要 SSE 格式
- DeerFlowClient 路径：Python 调用者需要同步 generator，零序列化开销

---

## 六、事件去重

多 stream mode 同时使用时，同一状态可能通过不同 mode 重复到达：

```
values 模式:   完整状态快照（包含所有消息）
messages 模式: 单条消息增量
```

**DeerFlowClient 的去重**:

```python
seen_ids: set[str]         # values 路径内部去重
streamed_ids: set[str]     # messages → values 跨模式去重
counted_usage_ids: set[str] # usage 幂等计数
```

---

## 相关源码

| 组件 | 文件 |
|------|------|
| StreamBridge 基类 | `backend/packages/harness/deerflow/runtime/stream_bridge/base.py` |
| 内存实现 | `backend/packages/harness/deerflow/runtime/stream_bridge/memory.py` |
| Run Worker | `backend/packages/harness/deerflow/runtime/runs/worker.py` |
| RunJournal | `backend/packages/harness/deerflow/runtime/journal.py` |
| Checkpointer Provider | `backend/packages/harness/deerflow/runtime/checkpointer/` |
| SSE 格式化 | `backend/app/gateway/services.py` |
| SSE Consumer | `backend/app/gateway/services.py` |

## 深入阅读

- [运行时设计决策](../docs/core/runtime/09-design-decisions.md)
- [运行时实现分析](../docs/core/runtime/10-implementation-analysis.md)
- [事件推送](../docs/core/runtime/05-event-streaming.md)
- [前端 SSE 处理](../docs/core/frontend/01-rendering-logic.md)
- [不使用 useStream 的流式实现](../docs/core/frontend/02-without-usestream.md)
