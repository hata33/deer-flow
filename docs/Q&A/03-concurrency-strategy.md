# Q&A 03: 并发策略

> 系统采用了什么样的并发策略？线程、协程、事件循环各自承担什么角色？

---

## 并发模型总览

DeerFlow 采用**混合并发模型**，在三个层次上使用不同的并发原语：

| 层次 | 并发原语 | 原因 |
|------|---------|------|
| HTTP 请求处理 | asyncio 协程 | FastAPI/ASGI 天然异步 |
| 子代理执行 | ThreadPoolExecutor + 独立事件循环 | 长时间运行、需要隔离 |
| 文件/阻塞 I/O | `asyncio.to_thread()` | 不阻塞主事件循环 |
| 同步工具调用 | ThreadPoolExecutor 桥接 | async→sync 适配 |

---

## 一、主事件循环（FastAPI/uvicorn）

### 架构

```
uvicorn (ASGI Server)
    │
    ├── 主事件循环 (asyncio)
    │   ├── HTTP 请求处理（FastAPI 路由）
    │   ├── SSE 流式推送（async generator）
    │   ├── LangGraph agent.astream()
    │   └── 数据库查询（async SQLAlchemy）
    │
    └── 如果可用: uvloop（高性能事件循环替代）
```

### 为什么用协程

- FastAPI 是 ASGI 框架，天然基于 asyncio
- HTTP 请求和 SSE 流都是 I/O 密集型，协程效率最高
- LangGraph 的 `agent.astream()` 本身是 async generator

### 关键代码路径

```python
# runtime/runs/worker.py — 主 Agent 流式执行
async for chunk in agent.astream(
    graph_input,
    config=runnable_config,
    stream_mode=single_mode,
):
    await bridge.publish(run_id, sse_event, serialize(chunk))
```

---

## 二、子代理执行：双线程池架构

这是系统中最复杂的并发设计。

### 架构图

```
主事件循环 (FastAPI)
    │
    │  submit()
    ▼
_scheduler_pool (ThreadPoolExecutor, max_workers=3)
    │
    │  在线程中启动
    ▼
_isolated_subagent_loop (独立的 asyncio 事件循环)
    │  运行在守护线程中
    │
    │  asyncio.run_coroutine_threadsafe()
    ▼
子代理执行 (agent.astream())
```

### 两个关键组件

**1. 调度线程池** (`subagents/executor.py`):

```python
_scheduler_pool = ThreadPoolExecutor(
    max_workers=3,                          # 最多 3 个并行子代理
    thread_name_prefix="subagent-scheduler-"
)
```

- 接收任务提交
- 限制并发数（`MAX_CONCURRENT_SUBAGENTS = 3`）
- 跟踪任务状态

**2. 独立事件循环** (`subagents/executor.py`):

```python
_isolated_subagent_loop: asyncio.AbstractEventLoop | None = None
_isolated_subagent_loop_thread: threading.Thread | None = None
```

- 运行在守护线程中（`subagent-persistent-loop`）
- 应用生命周期内持续存在
- 通过 `asyncio.run_coroutine_threadsafe()` 提交任务
- 通过 `copy_context()` 保持上下文变量

### 为什么不用主事件循环

1. **资源隔离**: 子代理创建的 httpx 客户端等资源绑定到事件循环。如果复用主循环，这些资源的生命周期难以管理
2. **避免饥饿**: 子代理是 CPU/IO 密集型，直接在主循环执行会影响 HTTP 响应延迟
3. **上下文保持**: `copy_context()` 跨线程传递 `ContextVar`，确保子代理能访问线程本地状态

### 任务生命周期

```
PENDING → RUNNING → COMPLETED
                 ├── FAILED
                 ├── TIMED_OUT (15 分钟)
                 └── CANCELLED
```

---

## 三、同步工具桥接

### 问题

部分工具（如 MCP 工具）是 async 的，但 LangGraph 的工具调用在某些上下文中是同步的。

### 解决方案

```python
# tools/sync.py
_SYNC_TOOL_EXECUTOR = ThreadPoolExecutor(
    max_workers=10,
    thread_name_prefix="tool-sync"
)
```

**桥接策略**:

```python
def make_sync_tool_wrapper(async_func):
    def wrapper(*args, **kwargs):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的循环 → 直接 asyncio.run()
            return asyncio.run(async_func(*args, **kwargs))

        # 有运行中的循环 → 在线程池中执行
        future = _SYNC_TOOL_EXECUTOR.submit(
            lambda: asyncio.run(async_func(*args, **kwargs))
        )
        return future.result()
    return wrapper
```

---

## 四、文件和阻塞 I/O

使用 `asyncio.to_thread()` 将阻塞操作移到线程池：

```python
# 在 async 上下文中读取技能文件
all_skills = await asyncio.to_thread(storage.load_skills, enabled_only=True)
```

这避免了文件 I/O 阻塞主事件循环。

---

## 五、并发限制

| 资源 | 限制 | 强制方式 |
|------|------|---------|
| 并行子代理 | 3 | `ThreadPoolExecutor(max_workers=3)` + `SubagentLimitMiddleware` |
| 同步工具线程 | 10 | `ThreadPoolExecutor(max_workers=10)` |
| Run 并发 | 每个 thread_id 1 个 | `RunManager` 互斥锁 |
| 子代理超时 | 15 分钟 | `asyncio.wait_for()` |

`SubagentLimitMiddleware` 在更上层截断超额的 `task` 工具调用——即使 LLM 一次产出 5 个 task 调用，也只执行前 3 个。

---

## 六、协作取消

```python
# 子代理执行中的取消检查
async for chunk in agent.astream(...):
    if result.cancel_event.is_set():  # threading.Event
        break
```

使用 `threading.Event` 作为跨线程的取消信号。主线程设置 `cancel_event`，子代理线程在每次流迭代时检查。

---

## 相关源码

| 组件 | 文件 |
|------|------|
| 子代理调度池 | `backend/packages/harness/deerflow/subagents/executor.py` |
| 同步工具桥接 | `backend/packages/harness/deerflow/tools/sync.py` |
| Run Worker | `backend/packages/harness/deerflow/runtime/runs/worker.py` |
| 子代理限制中间件 | `backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py` |
| Run Manager | `backend/packages/harness/deerflow/runtime/runs/manager.py` |

## 深入阅读

- [子代理设计决策](../docs/core/subagents/05-design-decisions.md)
- [运行时设计决策](../docs/core/runtime/09-design-decisions.md)
- [Agent 请求全流程](../docs/lifecycle/01-agent-request-flow.md)
