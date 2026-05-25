# 执行引擎

`SubagentExecutor` 是子代理系统的核心执行引擎，负责创建 LangChain Agent 实例并在隔离的上下文中运行任务。它支持同步和异步两种执行模式，并实现了完整的超时处理、协作式取消和 token 用量收集机制。

## SubagentExecutor 类

### 初始化

```python
executor = SubagentExecutor(
    config=SubagentConfig(name="bash", ...),  # 子代理配置
    tools=all_tools,                          # 父代理的全部工具（将被过滤）
    app_config=app_config,                    # 应用配置（可选，延迟加载）
    parent_model="gpt-4o",                    # 父代理模型名称
    sandbox_state=sandbox_state,              # 沙箱状态（透传给子代理）
    thread_data=thread_data,                  # 线程数据（透传给子代理）
    thread_id="abc123",                       # 线程 ID
    trace_id="trace-001",                     # 分布式追踪 ID
)
```

初始化阶段完成：
1. **工具过滤**：根据 `config.tools`（白名单）和 `config.disallowed_tools`（黑名单）过滤工具
2. **模型解析**（部分场景延迟）：如果配置和参数足够则立即解析，否则推迟到 `_create_agent()`
3. **追踪 ID**：继承父代理的 trace_id 或自动生成

### 工具过滤

`_filter_tools()` 实现两层过滤：

```
父代理全部工具
    │
    ▼ 白名单过滤（config.tools）
    │  None → 不过滤（保留全部）
    │  ["bash", "ls"] → 仅保留指定工具
    │
    ▼ 黑名单过滤（config.disallowed_tools）
    │  始终移除 "task"（默认值）
    │  可扩展移除其他工具
    │
    ▼ 技能工具约束（_apply_skill_allowed_tools）
    │  根据技能元数据的 allowed-tools 进一步限制
    │
    ▼ 最终工具列表
```

## 双线程池架构

子代理执行采用双线程池设计，确保异步 Agent 运行与调用方的事件循环隔离：

```
调用方线程（主代理事件循环）
    │
    │ execute_async(task, task_id)
    │
    ▼ _scheduler_pool (ThreadPoolExecutor, 3 workers)
    │   │
    │   │ run_task()
    │   │   ├── 更新状态为 RUNNING
    │   │   │
    │   │   ▼ _submit_to_isolated_loop_in_context()
    │   │       │
    │   │       │ copy_context() → 保留 ContextVar（user_id 等）
    │   │       │
    │   │       ▼ asyncio.run_coroutine_threadsafe()
    │   │           │
    │   │           ▼ 持久化事件循环（daemon thread）
    │   │               │
    │   │               │ _aexecute(task, result_holder)
    │   │               │   ├── _build_initial_state()
    │   │               │   ├── _create_agent()
    │   │               │   └── agent.astream()
    │   │               │
    │   │               ▼ Future<SubagentResult>
    │   │
    │   ▼ Future.result(timeout=config.timeout_seconds)
    │       │
    │       ├── 成功 → SubagentResult(COMPLETED)
    │       └── TimeoutError → SubagentResult(TIMED_OUT)
    │
    ▼ 返回 task_id
```

### 持久化事件循环

`_get_isolated_subagent_loop()` 管理一个长生命周期的 asyncio 事件循环：

- **首次调用**时创建新的事件循环和守护线程
- **后续调用**复用已有的循环
- **自动恢复**：如果检测到循环不可用（线程退出、循环关闭），自动重建
- **进程退出清理**：通过 `atexit` 注册清理函数

复用持久化循环的优势：
- 避免为每次子代理执行创建临时事件循环
- 共享的异步资源（如 httpx 连接池）不会因循环关闭而被销毁
- 减少 asyncio 事件循环的创建和销毁开销

### ContextVar 透传

`_submit_to_isolated_loop_in_context()` 使用 `contextvars.copy_context()` 捕获当前线程的上下文变量（如 `user_id`、`trace_id`），在持久化循环的线程中恢复这些变量：

```python
parent_context = copy_context()
future = parent_context.run(
    lambda: asyncio.run_coroutine_threadsafe(
        coro_factory(),
        _get_isolated_subagent_loop(),
    )
)
```

## 执行方法

### execute() — 同步执行

阻塞调用线程直到任务完成或超时。自动检测调用环境选择执行路径：

```
execute(task)
    │
    ├── 检测到运行中的事件循环
    │   └── _execute_in_isolated_loop(task)
    │       ├── _submit_to_isolated_loop_in_context()
    │       └── Future.result(timeout=config.timeout_seconds)
    │
    └── 无运行中的事件循环
        └── asyncio.run(_aexecute(task))
```

### execute_async() — 异步执行

提交到后台线程池后立即返回 task_id，由 task_tool 轮询获取结果：

```
execute_async(task, task_id)
    │
    ├── 创建 PENDING 状态的 SubagentResult
    ├── 存入 _background_tasks 全局字典
    │
    └── _scheduler_pool.submit(run_task)
        ├── 状态更新为 RUNNING
        ├── _submit_to_isolated_loop_in_context()
        ├── Future.result(timeout=config.timeout_seconds)
        │
        ├── 成功 → try_set_terminal(COMPLETED)
        ├── 超时 → try_set_terminal(TIMED_OUT)
        └── 异常 → try_set_terminal(FAILED)
```

### _aexecute() — 核心异步执行

所有执行路径最终都汇聚到 `_aexecute()` 方法：

```
_aexecute(task, result_holder)
    │
    ├── _build_initial_state(task)
    │   ├── _load_skills() → 加载并过滤技能
    │   ├── _apply_skill_allowed_tools() → 技能工具约束
    │   └── _load_skill_messages() → 技能内容注入
    │
    ├── _create_agent(filtered_tools) → 创建 LangChain Agent
    │
    ├── SubagentTokenCollector → 注册 token 用量回调
    │
    └── agent.astream(state, stream_mode="values")
        │
        ├── 前置取消检查（cancel_event.is_set()）
        │
        ├── 迭代中逐步收集 AI 消息（去重）
        │
        ├── 迭代边界检查取消信号
        │
        └── 提取最终结果
            ├── 最后一条 AIMessage 的 content
            ├── 回退到最后一条消息
            └── "No response generated"
```

## 超时处理

超时通过两层机制实现：

### Future 超时（硬超时）

```python
execution_future.result(timeout=self.config.timeout_seconds)
```

当超过 `timeout_seconds`（默认 900 秒）时：
1. `concurrent.futures.TimeoutError` 被抛出
2. 设置 `result_holder.cancel_event` 通知协作式取消
3. `try_set_terminal(TIMED_OUT)` 原子性地标记终态
4. `execution_future.cancel()` 尝试取消底层协程

### 协作式取消（软取消）

`cancel_event` 是一个 `threading.Event`，在 `agent.astream()` 的迭代边界被检查：

```python
async for chunk in agent.astream(...):
    if result.cancel_event.is_set():  # 协作式取消检查
        result.try_set_terminal(CANCELLED)
        return result
```

**注意**：取消仅在 astream 迭代边界被检测。如果单次迭代中的工具调用运行时间很长，取消信号需要等到下一个 chunk 产出才能生效。

## SSE 事件发射

执行过程中的 SSE 事件通过 `StreamWriter` 推送给前端：

```python
# task_tool 中的轮询逻辑
while not result.status.is_terminal:
    result = get_background_task_result(task_id)
    # 发射 SSE 事件
    await writer.write({"type": "task_running", "task_id": task_id})
    await asyncio.sleep(5)  # 5 秒轮询间隔

# 终态事件
if result.status == SubagentStatus.COMPLETED:
    await writer.write({"type": "task_completed", "result": result.result})
elif result.status == SubagentStatus.FAILED:
    await writer.write({"type": "task_failed", "error": result.error})
elif result.status == SubagentStatus.TIMED_OUT:
    await writer.write({"type": "task_timed_out", "error": result.error})
```

## Token 用量收集

每个子代理执行创建一个 `SubagentTokenCollector` 作为 LangChain 回调：

```python
collector = SubagentTokenCollector(caller=f"subagent:{config.name}")
run_config = {"callbacks": [collector]}
```

收集流程：
1. 每次 LLM 调用结束后触发 `on_llm_end()`
2. 从 `response.generations[].message.usage_metadata` 提取用量
3. 通过 `run_id` 去重，确保每个调用仅记录一次
4. 执行完成后通过 `collector.snapshot_records()` 获取全部记录
5. 存入 `SubagentResult.token_usage_records`
6. 最终通过 `RunJournal.record_external_llm_usage_records()` 合并到父代理

## 错误处理

### 异常分类

| 异常类型 | 处理方式 | 终态 |
|---------|---------|------|
| `FuturesTimeoutError` | 设置 cancel_event + TIMED_OUT | `TIMED_OUT` |
| `cancel_event.is_set()` | 协作式取消检查 | `CANCELLED` |
| 其他 `Exception` | 记录日志 + FAILED | `FAILED` |

### 终态原子性

`try_set_terminal()` 通过 `threading.Lock` 保证终态转换的原子性：

```python
def try_set_terminal(self, status):
    with self._state_lock:
        if self.status.is_terminal:  # 已是终态
            return False              # 拒绝后续写入
        self.status = status          # 第一次终态转换生效
        return True
```

这防止了超时线程和执行线程之间的竞态条件：无论哪个线程先到达，第一个成功的终态转换生效，后续的写入被拒绝。

## 全局状态管理

### 后台任务存储

```python
_background_tasks: dict[str, SubagentResult] = {}  # task_id → result
_background_tasks_lock = threading.Lock()            # 保护并发访问
```

### 任务生命周期管理函数

| 函数 | 用途 |
|------|------|
| `execute_async()` | 创建并提交后台任务 |
| `get_background_task_result(task_id)` | 获取任务结果（轮询用） |
| `list_background_tasks()` | 列出所有后台任务 |
| `request_cancel_background_task(task_id)` | 请求取消任务 |
| `cleanup_background_task(task_id)` | 清理已完成的任务 |

`cleanup_background_task()` 仅移除终态任务，避免与后台执行器的竞态条件。应由 `task_tool` 在轮询完成后调用，防止内存泄漏。
