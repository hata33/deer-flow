# 完整生命周期

本文档描述一个子代理任务从 `task()` 工具调用到最终结果返回的完整生命周期，涵盖注册、执行、事件发射、结果返回和错误处理的全过程。

## 生命周期总览

```
[注册阶段]
    内置代理定义 → 自定义代理合并 → config.yaml 覆盖 → 可用代理列表
         │
         ▼
[调用阶段]
    Lead Agent LLM 响应 → task 工具调用 → SubagentLimitMiddleware 并发检查
         │
         ▼
[执行阶段]
    SubagentExecutor.execute_async() → _scheduler_pool → 持久化事件循环
         │
         ▼
[监控阶段]
    task_tool 轮询（5s 间隔） → SSE 事件发射 → 前端实时更新
         │
         ▼
[完成阶段]
    终态（COMPLETED/FAILED/TIMED_OUT） → 结果返回 → token 用量合并 → 清理
```

## 1. 注册阶段

### 内置代理注册

系统启动时，`builtins/__init__.py` 定义内置代理注册表：

```python
# builtins/__init__.py
BUILTIN_SUBAGENTS = {
    "general-purpose": GENERAL_PURPOSE_CONFIG,
    "bash": BASH_AGENT_CONFIG,
}
```

### 自定义代理合并

`registry.py` 的 `get_subagent_names()` 合并内置和自定义代理：

```python
names = list(BUILTIN_SUBAGENTS.keys())          # ["general-purpose", "bash"]
for custom_name in subagents_config.custom_agents:
    if custom_name not in names:
        names.append(custom_name)                # 追加自定义代理
```

### config.yaml 覆盖应用

`get_subagent_config()` 对每个代理应用三层覆盖：

```
基础配置（内置或自定义）
    │
    ▼ per-agent 覆盖（config.yaml agents.<name>）
    │   - timeout_seconds, max_turns, model, skills
    │
    ▼ 全局默认（仅内置代理）
    │   - subagents.timeout_seconds, subagents.max_turns
    │
    ▼ 最终 SubagentConfig
```

### 沙箱过滤

`get_available_subagent_names()` 根据沙箱配置过滤可用代理：

```python
if not is_host_bash_allowed():
    names = [n for n in names if n != "bash"]
```

## 2. 调用阶段

### 主代理 LLM 响应

当 Lead Agent 判断某个子任务需要委派时，LLM 在响应中生成 `task` 工具调用：

```json
{
    "tool_calls": [{
        "name": "task",
        "args": {
            "description": "分析项目结构",
            "subagent_type": "general-purpose"
        }
    }]
}
```

### 并发控制

`SubagentLimitMiddleware` 在 `after_model` 阶段检查 `task` 调用数量：

```
LLM 响应中的 task 调用
    │
    ▼ 计数
    │
    ├── ≤ 3 个 → 全部通过
    │
    └── > 3 个 → 截断到前 3 个，记录警告日志
```

这确保了 `MAX_CONCURRENT_SUBAGENTS = 3` 的并发上限。

### task_tool 处理

`task` 工具（定义在 `tools/builtins/task.py`）执行以下步骤：

```
1. 查找代理配置: get_subagent_config(subagent_type)
2. 创建执行器: SubagentExecutor(config, tools, ...)
3. 提交后台执行: executor.execute_async(task, task_id)
4. 发射 task_started 事件
5. 进入轮询循环
```

## 3. 执行阶段

### 后台任务提交

`execute_async()` 创建 PENDING 状态的结果并提交到线程池：

```
execute_async(task, task_id)
    │
    ├── 创建 SubagentResult(status=PENDING)
    ├── 存入 _background_tasks[task_id]
    │
    └── _scheduler_pool.submit(run_task)
```

### 状态转换为 RUNNING

`run_task()` 在调度线程中将状态更新为 RUNNING：

```python
with _background_tasks_lock:
    _background_tasks[task_id].status = SubagentStatus.RUNNING
    _background_tasks[task_id].started_at = datetime.now()
```

### 持久化事件循环提交

通过 `copy_context()` 保留上下文变量，提交协程到持久化循环：

```
parent_context = copy_context()
execution_future = _submit_to_isolated_loop_in_context(
    parent_context,
    lambda: self._aexecute(task, result_holder),
)
```

### Agent 创建与执行

`_aexecute()` 中的 Agent 创建和执行流程：

```
_aexecute(task, result_holder)
    │
    ├── _build_initial_state(task)
    │   │
    │   ├── _load_skills()
    │   │   ├── skills=None → 加载全部已启用技能
    │   │   ├── skills=[] → 不加载任何技能
    │   │   └── skills=["a","b"] → 仅加载指定技能
    │   │
    │   ├── _apply_skill_allowed_tools()
    │   │   └── 根据技能元数据的 allowed-tools 过滤工具
    │   │
    │   ├── _load_skill_messages()
    │   │   └── 读取每个技能的 SKILL.md，包裹在 <skill> XML 标签中
    │   │
    │   └── 合并 system_prompt + skills → 单个 SystemMessage
    │       + HumanMessage(task)
    │
    ├── _create_agent(filtered_tools)
    │   ├── resolve_subagent_model_name() → 解析模型
    │   ├── create_chat_model() → 创建 LLM 实例
    │   ├── build_subagent_runtime_middlewares() → 中间件链
    │   └── create_agent(model, tools, middleware) → Agent 实例
    │
    └── agent.astream(state, stream_mode="values")
        │
        ├── 前置取消检查（cancel_event）
        │
        ├── 逐 chunk 迭代
        │   ├── 协作式取消检查（每 chunk）
        │   ├── 收集 AI 消息（去重）
        │   └── 更新 final_state
        │
        └── 提取最终结果
```

### 超时等待

调度线程通过 `Future.result(timeout=)` 等待执行完成：

```python
execution_future.result(timeout=self.config.timeout_seconds)
```

## 4. 事件发射阶段

### 轮询循环

`task_tool` 以 5 秒间隔轮询后台任务状态：

```
task_tool 轮询循环:
    │
    ├── 发射 task_started 事件（首次）
    │
    └── while not result.status.is_terminal:
        │
        ├── get_background_task_result(task_id)
        │
        ├── 发射 task_running 事件（含当前状态）
        │
        └── await asyncio.sleep(5)  # 5 秒轮询间隔
```

### SSE 事件格式

通过 `StreamWriter` 发射的 SSE 事件：

| 事件类型 | 触发时机 | 载荷 |
|---------|---------|------|
| `task_started` | 任务提交后 | `{task_id, subagent_type, description}` |
| `task_running` | 每次轮询 | `{task_id, status, elapsed_seconds}` |
| `task_completed` | 执行完成 | `{task_id, result, ai_messages, token_usage}` |
| `task_failed` | 执行异常 | `{task_id, error}` |
| `task_timed_out` | 执行超时 | `{task_id, error}` |

## 5. 结果返回阶段

### 终态判定

轮询循环在以下任一条件下终止：

```python
result.status.is_terminal  # COMPLETED / FAILED / TIMED_OUT / CANCELLED
```

### 结果提取

从 `SubagentResult` 中提取最终结果：

```python
if result.status == SubagentStatus.COMPLETED:
    final_text = result.result           # AI 生成的最终文本
    ai_messages = result.ai_messages     # 完整的 AI 消息列表
    token_records = result.token_usage_records
```

### Token 用量合并

token 用量从子代理收集器合并回父代理：

```
SubagentTokenCollector
    │ snapshot_records()
    ▼
SubagentResult.token_usage_records
    │
    ▼ RunJournal.record_external_llm_usage_records()
    │
    ▼ 父代理的 RunJournal（统一用量统计）
```

每条 token 记录包含：
- `source_run_id`: LangChain 运行 ID
- `caller`: `"subagent:{agent_name}"`
- `input_tokens`, `output_tokens`, `total_tokens`

### 后台任务清理

结果返回后，`cleanup_background_task()` 从全局字典中移除已完成的任务：

```python
cleanup_background_task(task_id)
    │
    ├── 检查任务是否存在
    ├── 检查是否处于终态
    │
    └── del _background_tasks[task_id]  # 防止内存泄漏
```

仅清理终态任务，避免与后台执行器的竞态条件。

## 6. 错误处理生命周期

### 超时场景

```
execute_async() 提交
    │
    ▼ _scheduler_pool
    │   │
    │   ▼ Future.result(timeout=900)
    │       │
    │       ├── 900 秒内完成 → COMPLETED
    │       │
    │       └── TimeoutError
    │           ├── cancel_event.set()  → 通知协作式取消
    │           ├── try_set_terminal(TIMED_OUT)
    │           └── execution_future.cancel()
    │
    ▼ task_tool 轮询检测到 TIMED_OUT
        │
        └── SSE: task_timed_out 事件
```

### 异常场景

```
_aexecute() 执行中
    │
    ├── _build_initial_state() 异常
    │   └── try_set_terminal(FAILED, error=str(e))
    │
    ├── _create_agent() 异常
    │   └── try_set_terminal(FAILED, error=str(e))
    │
    ├── agent.astream() 异常
    │   └── try_set_terminal(FAILED, error=str(e))
    │
    └── 结果提取异常
        └── try_set_terminal(FAILED, error=str(e))

task_tool 轮询检测到 FAILED
    │
    └── SSE: task_failed 事件
```

### 取消场景

```
request_cancel_background_task(task_id)
    │
    └── cancel_event.set()
        │
        └── _aexecute 中 astream 迭代边界检查
            │
            ├── cancel_event.is_set() == True
            │   ├── try_set_terminal(CANCELLED, error="Cancelled by user")
            │   └── return result
            │
            └── task_tool 轮询检测到 CANCELLED
                │
                └── SSE: task_failed 事件（含取消消息）
```

### 竞态处理

超时线程和执行线程可能同时尝试设置终态。`try_set_terminal()` 通过 `threading.Lock` 保证：

```python
with self._state_lock:
    if self.status.is_terminal:  # 已有终态
        return False              # 拒绝后续写入（第一个写入生效）
    self.status = status          # 设置新终态
    return True
```

## 7. 并发执行场景

当主代理同时委派多个子任务时：

```
Lead Agent LLM 响应
    │
    │ 3 个 task 工具调用（SubagentLimitMiddleware 确保不超过 3 个）
    │
    ├── task_1: SubagentExecutor(config_1).execute_async(task_1, id_1)
    ├── task_2: SubagentExecutor(config_2).execute_async(task_2, id_2)
    └── task_3: SubagentExecutor(config_3).execute_async(task_3, id_3)

_scheduler_pool (3 workers)
    │
    ├── worker_1 → 持久化循环 → agent_1.astream()
    ├── worker_2 → 持久化循环 → agent_2.astream()
    └── worker_3 → 持久化循环 → agent_3.astream()

task_tool 并行轮询
    │
    ├── 每 5 秒检查 task_1, task_2, task_3 状态
    ├── 独立发射 SSE 事件
    └── 各自完成后返回结果
```

所有后台任务共享同一个持久化事件循环（单线程），通过 `asyncio.run_coroutine_threadsafe()` 并发调度。`_scheduler_pool` 的 3 个工作线程对应 3 个并发的 `Future.result(timeout=)` 等待。

## 8. 资源生命周期

```
进程启动
    │
    ├── BUILTIN_SUBAGENTS 注册表初始化
    └── _scheduler_pool 创建（3 workers）

首次子代理执行
    │
    └── _get_isolated_subagent_loop() 创建持久化事件循环 + 守护线程

每次子代理执行
    │
    ├── SubagentResult 创建 → _background_tasks 存储
    ├── SubagentTokenCollector 创建 → callback 注册
    ├── LangChain Agent 创建 → astream 执行
    └── Agent 完成 → token_records 收集 → cleanup_background_task

进程退出
    │
    ├── atexit: _shutdown_isolated_subagent_loop()
    │   ├── loop.call_soon_threadsafe(loop.stop)
    │   ├── thread.join(timeout=1)
    │   └── loop.close()（条件满足时）
    │
    └── _scheduler_pool 自动关闭（ThreadPoolExecutor 析构）
```
