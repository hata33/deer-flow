# Q&A 05: Agent 执行的线程 / 协程 / 事件循环

> Agent 的执行，分别在什么情况下使用线程、协程和事件循环？三者的调度关系是怎样的？

---

## 三种执行模型的使用场景

| 执行模型 | 使用场景 | 为什么 |
|---------|---------|-------|
| **协程 (async/await)** | HTTP 处理、Agent 流式执行、DB 查询 | I/O 密集、高并发、ASGI 天然支持 |
| **线程池** | 子代理调度、同步工具桥接、文件 I/O | CPU/阻塞操作、async→sync 适配 |
| **独立事件循环** | 子代理实际执行 | 资源隔离、生命周期独立于主循环 |

---

## 一、协程：主要执行路径

### 1.1 HTTP 请求 → Agent 执行

整个请求处理链是纯 async 的：

```
FastAPI async route
    → RunManager.submit()        # async
    → worker.run_agent()         # async
    → agent.astream()            # async generator
    → bridge.publish()           # async
    → sse_consumer()             # async generator → SSE
```

### 1.2 LangGraph ReAct 循环

LangGraph 的 `agent.astream()` 本身就是 async generator：

```python
# runtime/runs/worker.py
async for chunk in agent.astream(
    graph_input,
    config=runnable_config,
    stream_mode=["values", "messages", "updates", "custom"],
):
    await bridge.publish(run_id, sse_event, serialize(chunk))
```

**每次 yield 对应一个 LangGraph 节点完成**（values 模式）或一个 token（messages 模式）。

### 1.3 数据库操作

使用 async SQLAlchemy：

```python
# persistence/thread_meta/sql.py
async def search(self, query: ThreadSearchQuery) -> list[ThreadMeta]:
    result = await self._db.execute(stmt)
    return result.scalars().all()
```

---

## 二、线程池：阻塞操作和桥接

### 2.1 子代理调度（`_scheduler_pool`）

```python
# subagents/executor.py
_scheduler_pool = ThreadPoolExecutor(
    max_workers=3,
    thread_name_prefix="subagent-scheduler-"
)
```

**为什么用线程而非协程**:
- 子代理执行需要创建独立的 asyncio 事件循环
- asyncio 不允许嵌套事件循环（`asyncio.run()` 在已有循环中会报错）
- 线程提供隔离的执行环境，天然支持新事件循环

**调度流程**:

```
主事件循环中调用
    ↓
_scheduler_pool.submit(execute_async, ...)
    ↓ 在线程中运行
获取/创建 _isolated_subagent_loop
    ↓
asyncio.run_coroutine_threadsafe(_aexecute(), loop)
    ↓ 在独立循环中运行
agent.astream()  # 子代理实际执行
```

### 2.2 同步工具桥接（`_SYNC_TOOL_EXECUTOR`）

```python
# tools/sync.py
_SYNC_TOOL_EXECUTOR = ThreadPoolExecutor(
    max_workers=10,
    thread_name_prefix="tool-sync"
)
```

**场景**: MCP 工具是 async 的，但 `DeerFlowClient`（同步接口）需要调用它们。

```python
def make_sync_tool_wrapper(async_func):
    def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        # 已有循环 → 在线程池中运行新的循环
        future = _SYNC_TOOL_EXECUTOR.submit(
            lambda: asyncio.run(async_func(*args, **kwargs))
        )
        return future.result()
    return wrapper
```

### 2.3 文件 I/O（`asyncio.to_thread`）

```python
# 在 async 上下文中安全读取文件
all_skills = await asyncio.to_thread(storage.load_skills, enabled_only=True)
```

---

## 三、独立事件循环：子代理执行

### 3.1 架构

```python
_isolated_subagent_loop: asyncio.AbstractEventLoop | None = None
_isolated_subagent_loop_thread: threading.Thread | None = None
```

**初始化（懒创建）**:

```python
def _get_or_create_loop():
    if _isolated_subagent_loop is None:
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_forever,
            daemon=True,
            name="subagent-persistent-loop"
        )
        thread.start()
        _isolated_subagent_loop = loop
        _isolated_subagent_loop_thread = thread
    return _isolated_subagent_loop
```

### 3.2 为什么不用主循环

| 问题 | 如果用主循环 | 用独立循环 |
|------|-------------|-----------|
| httpx 客户端生命周期 | 绑定到主循环，但子代理是临时的 | 绑定到独立循环，随循环持续 |
| 错误隔离 | 子代理异常可能影响 HTTP 服务 | 完全隔离 |
| 上下文变量 | 与 HTTP 请求混淆 | `copy_context()` 保持独立 |
| 优雅关闭 | 难以单独关闭子代理 | 可独立关闭 |

### 3.3 上下文传递

```python
# 跨线程传递 ContextVar
ctx = contextvars.copy_context()
result = loop.run_coroutine_threadsafe(
    ctx.run(_aexecute, ...),
    loop
)
```

---

## 四、三者调度关系

```
┌─────────────────────────────────────────┐
│           主事件循环 (FastAPI)            │
│                                          │
│  HTTP → Agent.astream() → StreamBridge  │
│            │                             │
│            │ task 工具调用               │
│            ▼                             │
│   _scheduler_pool.submit()               │
│            │                             │
│ ───────────┼────────────────────────────│
│            │  跨线程边界                  │
│ ───────────┼────────────────────────────│
│            ▼                             │
│   ┌──────────────────────────┐           │
│   │ 调度线程                  │           │
│   │  → _isolated_loop.run()  │           │
│   └──────────┬───────────────┘           │
│              │                            │
│              ▼                            │
│   ┌──────────────────────────┐           │
│   │ 独立事件循环 (守护线程)    │           │
│   │  → agent.astream()       │           │
│   │  → Tool 调用              │           │
│   │  → 结果返回到主循环        │           │
│   └──────────────────────────┘           │
└─────────────────────────────────────────┘
```

**关键调度规则**:
1. 主事件循环处理所有 HTTP 和 SSE
2. 子代理通过 `_scheduler_pool` 进入线程
3. 线程内使用 `_isolated_subagent_loop` 执行 async 代码
4. 结果通过 `concurrent.futures.Future` 回到主循环
5. 文件 I/O 通过 `asyncio.to_thread()` 在默认线程池执行

---

## 相关源码

| 组件 | 文件 |
|------|------|
| 子代理调度 | `backend/packages/harness/deerflow/subagents/executor.py` |
| Run Worker | `backend/packages/harness/deerflow/runtime/runs/worker.py` |
| 同步工具桥接 | `backend/packages/harness/deerflow/tools/sync.py` |
| Gateway 应用 | `backend/app/gateway/app.py` |

## 深入阅读

- [子代理设计决策](../docs/core/subagents/05-design-decisions.md)
- [子代理实现分析](../docs/core/subagents/06-implementation-analysis.md)
- [运行时设计决策](../docs/core/runtime/09-design-decisions.md)
