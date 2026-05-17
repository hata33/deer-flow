Runtime 层——为什么单独拆分、Runtime 注入机制、四个子模块的职责

---

## 一、Runtime 层为什么单独拆分

`deerflow/runtime/` 目录包含四个子模块，它们都不是 agent 本身的业务逻辑，而是**运行时的基础设施**：

```
runtime/
  ├─ runs/               ← 运行管理（RunRecord、RunManager、worker 执行编排）
  ├─ stream_bridge/      ← 发布订阅（生产消费解耦的异步队列）
  ├─ store/              ← 元数据存储（thread 列表索引）
  └─ serialization.py    ← 对象序列化（LangChain 对象 → JSON）
```

**拆分的原因**：这些模块被上层（Gateway）和下层（Agent）同时依赖，放在 agents/ 里会造成循环依赖，放在 gateway/ 里又让 agent 层无法访问。runtime 是中间层，被两边横向依赖：

```
gateway/  ──依赖──►  runtime/  ◄──依赖──  agents/
（HTTP 调度）          （基础设施）          （业务逻辑）
```

| 子模块 | 解决的问题 | 谁依赖它 |
|--------|-----------|----------|
| `runs/` | 运行生命周期管理（创建、状态追踪、取消、并发控制） | gateway services.py |
| `runs/worker.py` | agent 执行编排（Runtime 注入、stream_mode 映射、推流、取消检测） | gateway services.py |
| `stream_bridge/` | agent 执行和 SSE 推流的解耦 | worker.py + services.py |
| `store/` | thread 元数据的快速索引 | threads.py + services.py |
| `serialization.py` | LangChain 对象到 JSON 的转换 | worker.py + threads.py |

---

## 二、LangGraph Runtime 是什么概念

`Runtime` 是 LangGraph 框架提供的类（`from langgraph.runtime import Runtime`），不是 DeerFlow 自己定义的。它是一个**运行时上下文容器**，在 agent 执行期间向中间件和工具注入外部信息：

```python
# worker.py:83
runtime = Runtime(context={"thread_id": thread_id}, store=store)
```

### Runtime 的三个属性

| 属性 | 类型 | 存什么 | 谁写入 | 谁读取 |
|------|------|--------|--------|--------|
| `context` | `dict` | 运行时上下文（thread_id、sandbox_id） | worker.py 注入，sandbox 工具追加 | 中间件、工具 |
| `store` | `BaseStore` | LangGraph Store 实例 | worker.py 注入 | 工具（间接） |
| `state` | `dict` | 当前 agent 状态的引用 | LangGraph 框架自动绑定 | 工具 |
| `config` | `dict` | RunnableConfig 的引用 | LangGraph 框架自动绑定 | 工具 |

### Runtime 注入链

```
worker.py run_agent()
  │
  ├─ 创建 Runtime
  │   runtime = Runtime(context={"thread_id": thread_id}, store=store)
  │
  ├─ 注入到 config
  │   config["configurable"]["__pregel_runtime"] = runtime
  │
  └─ agent.astream(graph_input, config=config)
      │
      └─ LangGraph 框架在执行 agent 时
          ├─ 自动绑定 runtime.state = 当前 ThreadState
          ├─ 自动绑定 runtime.config = 当前 RunnableConfig
          │
          └─ 中间件和工具通过参数接收 runtime
              ├─ middleware.before_agent(state, runtime)
              ├─ middleware.after_model(state, runtime)
              └─ tool(runtime=...)  ← ToolRuntime 注入
```

### 为什么不直接传参数

没有 Runtime 的话，thread_id、store、sandbox_id 这些运行时信息需要层层传递：
- 中间件需要 thread_id（ThreadDataMiddleware、MemoryMiddleware、LoopDetectionMiddleware）
- 工具需要 thread_id + state（task_tool、sandbox 工具、present_file_tool）
- 从 worker → agent → 中间件/工具，层级很深

Runtime 把这些信息打包成一个容器，LangGraph 框架自动传递给中间件和工具的钩子方法，不需要手动层层传参

---

## 三、Runtime 的读取者

### 3.1 中间件读取 runtime.context

几乎所有中间件都从 `runtime.context` 获取 `thread_id`：

```python
# thread_data_middleware.py
def before_agent(self, state, runtime):
    thread_id = runtime.context.get("thread_id")  # 获取 thread_id

# memory_middleware.py
def after_agent(self, state, runtime):
    thread_id = runtime.context.get("thread_id")

# loop_detection_middleware.py
def after_model(self, state, runtime):
    thread_id = runtime.context.get("thread_id")
```

### 3.2 中间件读取 runtime.state

部分中间件需要读写当前 agent 状态：

```python
# sandbox/middleware.py — 写入 sandbox_id
runtime.context["sandbox_id"] = sandbox_id

# sandbox/tools.py — 读取并写入 sandbox 状态
sandbox_state = runtime.state.get("sandbox")
runtime.state["sandbox"] = {"sandbox_id": sandbox_id}
```

### 3.3 工具读取 runtime.state + runtime.context

```python
# task_tool.py — 从父 agent 继承状态
sandbox_state = runtime.state.get("sandbox")
thread_data = runtime.state.get("thread_data")
thread_id = runtime.context.get("thread_id")

# sandbox/tools.py — 沙箱工具需要 thread_id 和 state
thread_data = runtime.state.get("thread_data")
```

### 3.4 工具读取 runtime.config

```python
# task_tool.py — 获取父 agent 模型名和追踪 ID
metadata = runtime.config.get("metadata", {})
parent_model = metadata.get("model_name")
trace_id = metadata.get("trace_id")
```

---

## 四、runtime 层四个子模块详解

### 4.1 runs/ — 运行管理

| 文件 | 内容 |
|------|------|
| `schemas.py` | `RunStatus`（pending/running/success/error/interrupted）、`DisconnectMode`（cancel/continue） |
| `manager.py` | `RunRecord` 数据类 + `RunManager` 内存注册表（创建、状态转换、取消、并发策略） |
| `worker.py` | `run_agent()` 执行编排器（Runtime 注入、agent 创建、astream 循环、推流、取消检测） |

worker.py 是运行时层的核心，连接上层（gateway services.py）和下层（agents/）

### 4.2 stream_bridge/ — 发布订阅

| 文件 | 内容 |
|------|------|
| `base.py` | `StreamBridge` 抽象基类（publish/subscribe/publish_end/cleanup） |
| `memory.py` | `MemoryStreamBridge`（asyncio.Queue 实现，每 run_id 独立队列） |
| `async_provider.py` | `make_stream_bridge()` 工厂（根据配置创建实现） |

详见 [010-StreamBridge发布订阅模式.md](010-StreamBridge发布订阅模式.md)

### 4.3 store/ — 元数据存储

| 文件 | 内容 |
|------|------|
| `async_provider.py` | `make_store()` 工厂（与 checkpointer 共享后端配置） |
| `provider.py` | 同步版本的工厂 + 错误提示常量 |
| `_sqlite_utils.py` | SQLite 连接字符串解析、目录创建 |

### 4.4 serialization.py — 序列化

将 LangChain 消息对象、Pydantic 模型转为 JSON：

```
serialize(obj, mode="values")    → 完整状态字典，去除 __pregel_* 内部键
serialize(obj, mode="messages")  → (message_chunk, metadata) 元组
serialize_lc_object(obj)         → 递归 model_dump() / dict() 兜底
```

消费者：worker.py（推流时序列化）和 threads.py（REST API 响应时序列化）

---

## 五、worker.py 中的 Runtime 注入流程

```python
# worker.py run_agent()
async def run_agent(bridge, run_manager, record, *, checkpointer, store, agent_factory, ...):

    # 1. 标记运行中
    await run_manager.set_status(run_id, RunStatus.running)

    # 2. 发布 metadata 事件
    await bridge.publish(run_id, "metadata", {"run_id": run_id, "thread_id": thread_id})

    # 3. ★ 创建 Runtime 并注入到 config
    from langgraph.runtime import Runtime
    runtime = Runtime(context={"thread_id": thread_id}, store=store)
    config.setdefault("configurable", {})["__pregel_runtime"] = runtime

    # 4. 调用工厂创建 agent
    runnable_config = RunnableConfig(**config)
    agent = agent_factory(config=runnable_config)

    # 5. 附加 checkpointer 和 store
    agent.checkpointer = checkpointer
    agent.store = store

    # 6. 映射 stream_mode
    lg_modes = map_stream_modes(requested_modes)

    # 7. ★ 执行 agent 并推流
    async for chunk in agent.astream(graph_input, config=runnable_config, stream_mode=lg_modes):
        if record.abort_event.is_set():    # 取消检测
            break
        await bridge.publish(run_id, sse_event, serialize(chunk))

    # 8. 设置最终状态
    await run_manager.set_status(run_id, RunStatus.success / interrupted / error)

    # 9. 清理
    await bridge.publish_end(run_id)
    asyncio.create_task(bridge.cleanup(run_id, delay=60))
```

### Runtime 注入的位置

Runtime 在 **第 3 步**创建并注入到 config，然后：
- agent.astream() 接收这个 config
- LangGraph 框架在执行 agent 时，将 config 中的 `__pregel_runtime` 传递给每个中间件和工具
- 中间件通过 `runtime` 参数获取，工具通过 `ToolRuntime` 类型参数获取

---

## 六、总结

| 概念 | 是什么 | 谁提供 |
|------|--------|--------|
| **Runtime 层**（runtime/） | DeerFlow 的运行时基础设施模块（运行管理、推流、存储、序列化） | DeerFlow 自己 |
| **Runtime 对象**（langgraph.runtime.Runtime） | LangGraph 的运行时上下文容器（context/state/config/store） | LangGraph 框架 |

Runtime 层被拆出来是因为它是被 gateway 和 agents 双向依赖的中间基础设施。Runtime 对象是 LangGraph 框架提供的依赖注入容器，在 worker.py 中创建并注入到 config，让中间件和工具能访问 thread_id、sandbox 等运行时信息，而不需要手动层层传参

---

> 本文档：runtime/ 是 DeerFlow 的基础设施层，包含四个子模块（runs/stream_bridge/store/serialization），被 gateway 和 agents 双向依赖所以独立拆分。LangGraph Runtime 是框架提供的运行时上下文容器，在 worker.py 中创建并注入到 config，中间件和工具通过它访问 thread_id、sandbox_state、model_name 等运行时信息
