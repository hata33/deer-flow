# 06 - 实现机制深度分析

> 本文档基于 `backend/packages/harness/deerflow/subagents/` 目录下的源码，逐层拆解子代理系统的实现细节。回答的是"代码怎么写的、为什么这么写"。

---

## 一、模块依赖关系图

```
┌─────────────────────────────────────────────────────────────────┐
│                      调用方（Agent 工具层）                       │
│                                                                  │
│  tools/builtins/task_tool.py                                     │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ task() 工具                                               │   │
│  │   → SubagentExecutor.execute_async(task, task_id)        │   │
│  │   → 每 5s 轮询 get_background_task_result(task_id)       │   │
│  │   → SSE 事件发射: task_started → task_running → 终态      │   │
│  └──────────┬───────────────────────────────────────────────┘   │
│             │                                                    │
└─────────────┼────────────────────────────────────────────────────┘
              │
┌─────────────▼────────────────────────────────────────────────────┐
│                    subagents 包（核心层）                          │
│                                                                   │
│  __init__.py ─── 统一导出                                         │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ executor.py  ── 执行引擎                                  │   │
│  │                                                           │   │
│  │ SubagentExecutor                                          │   │
│  │   ├─ execute()       同步执行（检测事件循环选路径）         │   │
│  │   ├─ execute_async() 异步执行（_scheduler_pool → 持久循环）│   │
│  │   └─ _aexecute()     核心异步方法（agent.astream 流式）    │   │
│  │                                                           │   │
│  │ SubagentResult + SubagentStatus                           │   │
│  │   └─ try_set_terminal()  线程安全终态转换                 │   │
│  │                                                           │   │
│  │ 全局状态:                                                  │   │
│  │   _scheduler_pool (3 workers)                             │   │
│  │   _isolated_subagent_loop (持久化事件循环)                 │   │
│  │   _background_tasks: dict[task_id → SubagentResult]      │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                   │
│  ┌──────────────────┐   ┌───────────────────────────┐           │
│  │ registry.py      │   │ config.py                 │           │
│  │                  │   │                           │           │
│  │ 内置 + 自定义合并 │   │ SubagentConfig 数据类     │           │
│  │ config.yaml 覆盖  │   │ resolve_subagent_model_name│          │
│  │ 沙箱可用性过滤    │   │                           │           │
│  └────────┬─────────┘   └───────────────────────────┘           │
│           │                                                     │
│  ┌────────▼──────────────────────────────────────────┐         │
│  │ builtins/ 子包                                     │         │
│  │                                                    │         │
│  │ general_purpose.py ── GENERAL_PURPOSE_CONFIG       │         │
│  │ bash_agent.py     ── BASH_AGENT_CONFIG             │         │
│  │ __init__.py       ── BUILTIN_SUBAGENTS 注册表      │         │
│  └────────────────────────────────────────────────────┘         │
│                                                                   │
│  ┌──────────────────────────────┐                               │
│  │ token_collector.py           │                               │
│  │ SubagentTokenCollector       │                               │
│  │ on_llm_end → 按 run_id 去重  │                               │
│  │ snapshot_records → 用量快照   │                               │
│  └──────────────────────────────┘                               │
└───────────────────────────────────────────────────────────────────┘
```

---

## 二、第 1 层：SubagentExecutor 生命周期

### 2.1 execute_async() 完整流程

```
execute_async(task="分析代码覆盖率", task_id="a1b2c3d4")
  │
  ├─ ① 创建 SubagentResult(PENDING, task_id="a1b2c3d4")
  │     → _background_tasks["a1b2c3d4"] = result
  │
  ├─ ② copy_context()  ← 捕获当前线程的 ContextVar 快照
  │
  └─ ③ _scheduler_pool.submit(run_task)
       │
       │  ─── 在 _scheduler_pool 线程中 ───
       │
       ├─ status = RUNNING, started_at = now
       │
       ├─ _submit_to_isolated_loop_in_context(
       │    parent_context,
       │    lambda: _aexecute(task, result_holder)
       │  )
       │    │
       │    │  ─── 在持久化事件循环线程中 ───
       │    │
       │    ├─ _build_initial_state(task)
       │    │   ├─ _load_skills()          ← asyncio.to_thread 加载
       │    │   ├─ _apply_skill_allowed_tools()
       │    │   ├─ _load_skill_messages()  ← 读取 SKILL.md
       │    │   └─ SystemMessage(prompt + skills) + HumanMessage(task)
       │    │
       │    ├─ _create_agent(filtered_tools)
       │    │   ├─ resolve_subagent_model_name()
       │    │   ├─ create_chat_model()
       │    │   └─ create_agent(model, tools, middleware)
       │    │
       │    ├─ SubagentTokenCollector(caller="subagent:general-purpose")
       │    │
       │    └─ async for chunk in agent.astream(state, config):
       │        ├─ cancel_event.is_set()? → CANCELLED
       │        ├─ 提取 AIMessage → ai_messages.append()
       │        └─ final_state = chunk
       │
       │    → try_set_terminal(COMPLETED, result=..., token_usage_records=...)
       │
       └─ Future.result(timeout=900)
           ├─ 正常完成 → result 已被 _aexecute 设置
           └─ FuturesTimeoutError → cancel_event.set() + TIMED_OUT
```

### 2.2 execute() 同步路径选择

```python
def execute(self, task, result_holder=None):
    loop = asyncio.get_running_loop()  # 可能抛 RuntimeError
    if loop is not None and loop.is_running():
        # 路径 A：已在事件循环中 → 持久化循环路径
        return self._execute_in_isolated_loop(task, result_holder)
    else:
        # 路径 B：不在事件循环 → asyncio.run()
        return asyncio.run(self._aexecute(task, result_holder))
```

路径 A 用于 LangGraph 工具调用（主 Agent 在 ASGI 事件循环中）。路径 B 用于测试和脚本场景。

---

## 三、第 2 层：SubagentResult 状态机

### 3.1 状态流转图

```
                    acquire()
                       │
                       ▼
                 ┌──────────┐
                 │ PENDING  │  已提交，等待执行
                 └────┬─────┘
                      │ run_task() 开始
                      ▼
                 ┌──────────┐
                 │ RUNNING  │  正在执行 agent.astream()
                 └─┬──┬──┬─┘
                   │  │  │
        ┌──────────┘  │  └──────────┐
        ▼             ▼             ▼
  ┌───────────┐ ┌──────────┐ ┌──────────┐
  │ COMPLETED │ │  FAILED  │ │ TIMED_OUT│
  │           │ │          │ │          │
  │ 正常完成   │ │ 异常失败  │ │ 超时终止  │
  └───────────┘ └──────────┘ └──────────┘

        +─── CANCELLED ← cancel_event.set() 后在 astream 迭代边界检测
```

### 3.2 终态原子性保证

```python
# 超时线程和执行线程可能同时到达：
#   Thread A (timeout): try_set_terminal(TIMED_OUT)
#   Thread B (execute): try_set_terminal(COMPLETED)

def try_set_terminal(self, status, **kwargs):
    with self._state_lock:              # 互斥锁
        if self.status.is_terminal:     # 已被其他线程设置
            return False                # 第二个写入被拒绝
        self.status = status
        self.result = kwargs.get("result")
        self.error = kwargs.get("error")
        self.completed_at = datetime.now()
        return True                     # 第一个写入成功
```

这种设计保证：如果超时先到达，执行结果被丢弃（返回 TIMED_OUT）；如果执行先完成，超时无效（返回 COMPLETED）。

---

## 四、第 3 层：Token 收集

### 4.1 SubagentTokenCollector 工作流程

```
agent.astream() 每次调用 LLM：
  │
  ├─ LLM 请求发送
  ├─ LLM 响应返回
  │   └─ on_llm_end(response, run_id=UUID)
  │       ├─ run_id 已在 _counted_run_ids? → 跳过（去重）
  │       ├─ 提取 response.generations[0][0].message.usage_metadata
  │       │   {input_tokens: 120, output_tokens: 85, total_tokens: 205}
  │       └─ _records.append({
  │             source_run_id: str(run_id),
  │             caller: "subagent:general-purpose",
  │             input_tokens: 120,
  │             output_tokens: 85,
  │             total_tokens: 205
  │           })
  │
  └─ 执行完成后：
      collector.snapshot_records() → list[dict]
      → SubagentResult.token_usage_records
      → RunJournal.record_external_llm_usage_records() 合并到父代理
```

**为什么按 run_id 去重**：LangChain 可能为同一次 LLM 调用触发多次 `on_llm_end`（例如重试或中间件触发）。`_counted_run_ids` 集合确保每次 LLM 调用仅记录一次。

### 4.2 用量记录格式

```python
{
    "source_run_id": "uuid-of-llm-call",    # LLM 调用唯一 ID
    "caller": "subagent:general-purpose",    # 来源标识
    "input_tokens": 120,                     # 输入 token
    "output_tokens": 85,                     # 输出 token
    "total_tokens": 205                      # 总计（若未提供则 input+output）
}
```

---

## 五、第 4 层：Registry — 注册与发现

### 5.1 配置解析优先级

```
get_subagent_config("general-purpose")
  │
  ├─ 第一步：查找内置代理
  │   BUILTIN_SUBAGENTS["general-purpose"] → SubagentConfig(...)
  │
  ├─ 第二步：查找 config.yaml custom_agents
  │   （仅在内置中未找到时）
  │   subagents.custom_agents["my-agent"] → SubagentConfig(...)
  │
  └─ 第三步：应用 config.yaml 覆盖
      │
      ├─ per-agent 覆盖（agents.general-purpose.*）
      │   timeout_seconds: 600     → overrides["timeout_seconds"] = 600
      │   max_turns: 30            → overrides["max_turns"] = 30
      │   model: "gpt-4o"          → overrides["model"] = "gpt-4o"
      │
      ├─ 全局默认值（仅内置代理，不影响自定义代理自身值）
      │   subagents.timeout_seconds: 1200  → 仅对内置代理生效
      │   subagents.max_turns: 80          → 仅对内置代理生效
      │
      └─ dataclasses.replace(config, **overrides)
```

### 5.2 沙箱可用性过滤

```python
def get_available_subagent_names(*, app_config=None):
    names = get_subagent_names()  # 内置 + 自定义

    if not is_host_bash_allowed():
        # 本地沙箱 + 未启用 allow_host_bash → 隐藏 bash 子代理
        names = [n for n in names if n != "bash"]

    return names
```

这确保前端和 `task()` 工具只看到当前运行时可用的代理。bash 子代理依赖宿主机 bash 执行能力，在受限制的沙箱模式下不可用。

### 5.3 内置代理对比

| 属性 | general-purpose | bash |
|------|----------------|------|
| **tools** | None（继承全部） | `["bash","ls","read_file","write_file","str_replace"]` |
| **disallowed_tools** | `["task","ask_clarification","present_files"]` | 同左 |
| **max_turns** | 100 | 60 |
| **timeout_seconds** | 900（15 分钟） | 900 |
| **model** | "inherit" | "inherit" |
| **system_prompt** | 通用任务 + 输出格式 | 命令执行专家 + 工作目录指引 |
| **适用场景** | 复杂多步骤任务 | 命令序列（git/npm/docker） |

---

## 六、第 5 层：SSE 事件发射

### 6.1 事件时序

```
task_tool 被调用
  │
  ├─ emit("task_started", {task_id, subagent_type, description})
  │
  ├─ SubagentExecutor.execute_async(task, task_id)
  │   返回 task_id
  │
  ├─ 循环：每 5s 轮询 get_background_task_result(task_id)
  │   │
  │   ├─ status == PENDING/RUNNING?
  │   │   → emit("task_running", {task_id, status, elapsed})
  │   │   → sleep 5s
  │   │
  │   ├─ status == COMPLETED?
  │   │   → emit("task_completed", {task_id, result})
  │   │   → cleanup_background_task(task_id)
  │   │   → break
  │   │
  │   ├─ status == FAILED?
  │   │   → emit("task_failed", {task_id, error})
  │   │   → cleanup_background_task(task_id)
  │   │   → break
  │   │
  │   └─ status == TIMED_OUT?
  │       → emit("task_timed_out", {task_id, error})
  │       → cleanup_background_task(task_id)
  │       → break
  │
  └─ 返回最终结果给 Agent
```

### 6.2 后台任务清理

```python
def cleanup_background_task(task_id):
    with _background_tasks_lock:
        result = _background_tasks.get(task_id)
        if result.status.is_terminal:     # 仅清理终态任务
            del _background_tasks[task_id]
```

延迟清理（而非 execute_async 完成时立即清理）避免后台执行器与清理操作之间的竞态条件。task_tool 在轮询到终态后调用 cleanup，确保 `_background_tasks` 字典不会无限增长。

---

## 七、文件职责速查表

| 文件 | 核心职责 | 关键类/函数 |
|------|----------|------------|
| `executor.py` | 执行引擎 + 状态管理 + 全局线程池 | `SubagentExecutor`、`SubagentResult`、`_scheduler_pool` |
| `registry.py` | 代理注册与发现 + 配置覆盖 | `get_subagent_config()`、`get_available_subagent_names()` |
| `config.py` | 配置数据类 + 模型名称解析 | `SubagentConfig`、`resolve_subagent_model_name()` |
| `token_collector.py` | LLM token 用量收集 | `SubagentTokenCollector.on_llm_end()` |
| `builtins/general_purpose.py` | 通用子代理配置 | `GENERAL_PURPOSE_CONFIG` |
| `builtins/bash_agent.py` | Bash 子代理配置 | `BASH_AGENT_CONFIG` |
| `builtins/__init__.py` | 内置代理注册表 | `BUILTIN_SUBAGENTS` 字典 |
