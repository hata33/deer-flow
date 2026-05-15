deer-flow-main\backend\packages\harness\deerflow\runtime\runs\worker.py

承接 004 中 `asyncio.create_task(run_agent(...))` 的调用，本步骤进入 agent 实际执行的核心逻辑

函数签名：

``` python
async def run_agent(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    *,
    checkpointer: Any,
    store: Any | None = None,
    agent_factory: Any,
    graph_input: dict,
    config: dict,
    stream_modes: list[str] | None = None,
    stream_subgraphs: bool = False,
    interrupt_before: list[str] | Literal["*"] | None = None,
    interrupt_after: list[str] | Literal["*"] | None = None,
) -> None:
```

参数都是 004 中 `start_run` 准备好的：`bridge` 推流桥、`run_manager` 运行管理、`record` 运行记录、`checkpointer` 状态持久化、`store` 线程存储、`agent_factory` agent 构建工厂、`graph_input` 图输入、`config` 运行配置、`stream_modes` 流式模式

---

**1. 标记运行中 + 记录回滚点**

``` python
await run_manager.set_status(run_id, RunStatus.running)
```

将 run 状态从 `pending` 转为 `running`

``` python
pre_run_checkpoint_id = None
try:
    config_for_check = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    ckpt_tuple = await checkpointer.aget_tuple(config_for_check)
    if ckpt_tuple is not None:
        pre_run_checkpoint_id = getattr(ckpt_tuple, "config", {}).get("configurable", {}).get("checkpoint_id")
except Exception:
    logger.debug("Could not get pre-run checkpoint_id for run %s", run_id)
```

记录运行前的检查点 ID，用于后续 rollback 时回退到运行前状态

---

**2. 发布元数据**

``` python
await bridge.publish(run_id, "metadata", {"run_id": run_id, "thread_id": thread_id})
```

向前端推送 `metadata` 事件，`useStream` hook 需要从中提取 `run_id` 和 `thread_id`

---

**3. 构建 Agent**

``` python
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

runtime = Runtime(context={"thread_id": thread_id}, store=store)
config.setdefault("configurable", {})["__pregel_runtime"] = runtime

runnable_config = RunnableConfig(**config)
agent = agent_factory(config=runnable_config)
```

创建 LangGraph `Runtime`，注入 `thread_id` 上下文和 `store`，嵌入 config 的 `configurable` 中。然后调用 `agent_factory`（即 `make_lead_agent`）构建 agent 图

``` python
if checkpointer is not None:
    agent.checkpointer = checkpointer
if store is not None:
    agent.store = store
if interrupt_before:
    agent.interrupt_before_nodes = interrupt_before
if interrupt_after:
    agent.interrupt_after_nodes = interrupt_after
```

给 agent 挂上检查点、存储、中断节点配置

---

**4. 构建流式模式**

``` python
lg_modes: list[str] = []
for m in requested_modes:
    if m == "messages-tuple":
        lg_modes.append("messages")
    elif m == "events":
        continue
    elif m in _VALID_LG_MODES:
        lg_modes.append(m)
if not lg_modes:
    lg_modes = ["values"]
```

将前端请求的 stream_mode 映射为 LangGraph 原生模式：`messages-tuple` → `messages`，`events` 不支持直接跳过，其余合法模式透传。去重后保持顺序

`_VALID_LG_MODES` 包含：`values`、`updates`、`checkpoints`、`tasks`、`debug`、`messages`、`custom`

---

**5. 流式执行（核心循环）**

这里分两个分支，取决于 `stream_mode` 的数量和是否启用了子图：

``` python
if len(lg_modes) == 1 and not stream_subgraphs:
    single_mode = lg_modes[0]
    async for chunk in agent.astream(graph_input, config=runnable_config, stream_mode=single_mode):
        if record.abort_event.is_set():
            break
        sse_event = _lg_mode_to_sse_event(single_mode)
        await bridge.publish(run_id, sse_event, serialize(chunk, mode=single_mode))
```

**单模式 + 无子图**：`agent.astream(stream_mode="values")` 传入单个字符串，LangGraph 直接返回原始 chunk，不需要解包。流程：`chunk → serialize → bridge.publish`

每轮循环检查 `abort_event`（用户取消信号），未取消则序列化后通过 bridge 发布

``` python
else:
    async for item in agent.astream(
        graph_input, config=runnable_config,
        stream_mode=lg_modes, subgraphs=stream_subgraphs,
    ):
        if record.abort_event.is_set():
            break
        mode, chunk = _unpack_stream_item(item, lg_modes, stream_subgraphs)
        if mode is None:
            continue
        sse_event = _lg_mode_to_sse_event(mode)
        await bridge.publish(run_id, sse_event, serialize(chunk, mode=mode))
```

**多模式 或 启用子图**：`agent.astream(stream_mode=["values", "messages"])` 传入列表，LangGraph 返回的不再是原始 chunk，而是带 mode 标记的元组：

```
("values", chunk1)
("messages", chunk2)
("values", chunk3)
...
```

所以需要 `_unpack_stream_item` 从元组里拆出 mode 和 chunk，再分别序列化发布。如果启用了子图（`stream_subgraphs=True`），元组还会多一个命名空间前缀变成 `(_ns, mode, chunk)` 三元组

两个分支做的事情完全一样——从 `agent.astream` 拿到数据、检查是否被取消、序列化、推入 bridge。区别只在于 LangGraph 的返回格式不同：单模式直接给数据，多模式给 `(mode, data)` 元组需要多一步拆包

---

**6. 最终状态**

``` python
if record.abort_event.is_set():
    action = record.abort_action
    if action == "rollback":
        await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
        # TODO: 实现完整的检查点回滚
    else:
        await run_manager.set_status(run_id, RunStatus.interrupted)
else:
    await run_manager.set_status(run_id, RunStatus.success)
```

执行结束后根据是否被中断设置最终状态：
- 正常完成 → `success`
- 被中断（interrupt）→ `interrupted`
- 被回滚（rollback）→ `error`，后续需回退检查点（Phase 2 待实现）

---

**7. 异常处理**

``` python
except asyncio.CancelledError:
    if action == "rollback":
        await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
    else:
        await run_manager.set_status(run_id, RunStatus.interrupted)

except Exception as exc:
    await run_manager.set_status(run_id, RunStatus.error, error=error_msg)
    await bridge.publish(run_id, "error", {"message": error_msg, "name": type(exc).__name__})
```

`CancelledError`：task 被取消时的处理，逻辑与中断一致。其他异常：标记 `error` 并向前端推送 error 事件

---

**8. 清理收尾**

``` python
finally:
    await bridge.publish_end(run_id)
    asyncio.create_task(bridge.cleanup(run_id, delay=60))
```

无论成功还是失败，都发布 `end` 哨兵通知消费者结束，然后延迟 60 秒清理 bridge 中的队列资源

---

**RunStatus 状态流转**（定义在 `deerflow/runtime/runs/schemas.py`）：

```
pending → running → success
                  → interrupted
                  → error
                  → timeout
```

**StreamBridge 生产消费模型**（定义在 `deerflow/runtime/stream_bridge/base.py`）：

- `bridge.publish(run_id, event, data)` — 生产侧：agent 执行过程中将事件推入队列
- `bridge.subscribe(run_id)` — 消费侧：`sse_consumer` 异步迭代取出事件，格式化为 SSE 推送前端
- `bridge.publish_end(run_id)` — 发送结束哨兵，消费者收到后关闭流
- `bridge.cleanup(run_id, delay)` — 延迟释放队列，给迟到的消费者留缓冲时间

**序列化**（`deerflow/runtime/serialization.py`）：`serialize(chunk, mode)` 根据 mode 将 LangGraph 内部对象转为 JSON 可序列化结构。`messages` 模式走消息元组序列化，`values` 模式走全状态序列化（过滤 `__pregel_*` 内部键）

> 本步骤：在后台 task 中运行 agent 图 → 流式产出 chunk → 通过 StreamBridge 发布 → 由 sse_consumer 消费推送到前端，直至完成或被中断/取消
