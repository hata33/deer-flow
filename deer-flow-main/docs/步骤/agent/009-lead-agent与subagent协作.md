Lead Agent 与 Subagent 协作——编排模式、调用流程、并行控制与执行机制

---

## 一、整体架构

Lead agent 是**编排者（Orchestrator）**，不直接做复杂工作，而是将任务分解后通过 `task` 工具委派给 subagent 并行执行。Subagent 在独立上下文中运行，完成后将结果返回给 lead agent 综合

```
Lead Agent（编排者）
  │
  ├─ 分析用户请求 → 分解为并行子任务
  │
  ├─ 调用 task() × N（并行）
  │   ├─ task(description="分析财务", prompt="...", subagent_type="general-purpose")
  │   ├─ task(description="搜索新闻", prompt="...", subagent_type="general-purpose")
  │   └─ task(description="行业趋势", prompt="...", subagent_type="general-purpose")
  │
  ├─ 等待所有 task 返回结果
  │
  └─ 综合所有结果 → 返回用户
```

---

## 二、关键文件

| 文件 | 职责 |
|------|------|
| `tools/builtins/task_tool.py` | `task` 工具定义，lead agent 调用 subagent 的入口 |
| `subagents/executor.py` | `SubagentExecutor`，双线程池架构，创建并执行 subagent |
| `subagents/config.py` | `SubagentConfig` 数据类 |
| `subagents/registry.py` | 子代理注册表，管理可用类型 |
| `subagents/builtins/general_purpose.py` | general-purpose 子代理配置 |
| `subagents/builtins/bash_agent.py` | bash 子代理配置 |
| `agents/middlewares/subagent_limit_middleware.py` | 截断超限 task 调用 |
| `agents/lead_agent/prompt.py` | `_build_subagent_section()` 生成编排指令 |

---

## 三、两种内置 Subagent

### 3.1 配置对比

| | general-purpose | bash |
|---|----------------|------|
| **用途** | 复杂多步任务（调研、分析、代码探索） | 命令执行（git/build/test/deploy） |
| **工具** | `None`（继承父 agent 全部工具） | `["bash", "ls", "read_file", "write_file", "str_replace"]` |
| **禁止工具** | `["task", "ask_clarification", "present_files"]` | 同左 |
| **模型** | `"inherit"`（与父 agent 相同） | `"inherit"` |
| **最大轮次** | 50 | 30 |
| **超时** | 900s（15 分钟） | 900s |

### 3.2 禁止工具的原因

- **`task`**：防止递归嵌套（subagent 不能再创建 subagent）
- **`ask_clarification`**：subagent 在独立上下文中运行，无法与用户交互
- **`present_files`**：文件展示由 lead agent 统一管理

### 3.3 SubagentConfig 数据结构

**文件**：`subagents/config.py`

```python
@dataclass
class SubagentConfig:
    name: str                          # 唯一标识
    description: str                   # 描述何时使用
    system_prompt: str                 # 行为指导
    tools: list[str] | None = None     # 允许的工具（None = 继承全部）
    disallowed_tools: list[str] = ["task"]  # 禁止的工具
    model: str = "inherit"             # "inherit" 或指定模型名
    max_turns: int = 50                # 最大轮次
    timeout_seconds: int = 900         # 超时秒数
```

### 3.4 可用性过滤

**文件**：`subagents/registry.py` → `get_available_subagent_names()`

```python
def get_available_subagent_names():
    names = list(BUILTIN_SUBAGENTS.keys())  # ["general-purpose", "bash"]
    if not is_host_bash_allowed():
        names = [n for n in names if n != "bash"]  # bash 子代理需宿主机权限
    return names
```

---

## 四、Lead Agent 何时创建 Subagent

### 4.1 触发条件

Subagent 的使用完全由 LLM 自主决定，通过系统提示词中的 `<subagent_system>` 段落指导

**文件**：`agents/lead_agent/prompt.py` → `_build_subagent_section(max_concurrent)`

### 4.2 使用场景

**用 subagent**：
- 复杂调研：需要多个信息源并行搜索
- 多维度分析：不同角度同时探索（如对比 5 个云服务商）
- 大型代码库：不同部分并行分析
- 构建测试部署：bash 子代理执行命令序列
- 任务可分解为 2+ 个并行子任务

**不用 subagent**：
- 单步操作（直接用工具）
- 简单问答
- 需要用户交互（subagent 无 ask_clarification）
- 顺序依赖任务（上一步结果决定下一步）
- 不足 3 步的简单任务

### 4.3 编排指令（提示词摘要）

`<subagent_system>` 段落的核心规则：

```
硬性约束：每次响应最多 N 个 task 调用
超出被系统静默丢弃（SubagentLimitMiddleware 强制执行）

多批次执行（子任务 > N 时）：
  Turn 1: 启动前 N 个 → 等结果
  Turn 2: 启动下一批 → 等结果
  Final: 综合所有结果

工作流：COUNT → PLAN BATCHES → EXECUTE → REPEAT → SYNTHESIZE
```

---

## 五、并行控制

### 5.1 SubagentLimitMiddleware

**文件**：`agents/middlewares/subagent_limit_middleware.py`

在 `after_model()` 中强制截断超限的 task 调用：

```python
class SubagentLimitMiddleware(AgentMiddleware):
    def _truncate_task_calls(self, state):
        tool_calls = last_msg.tool_calls
        task_indices = [i for i, tc in enumerate(tool_calls) if tc["name"] == "task"]

        if len(task_indices) <= self.max_concurrent:
            return None  # 未超限

        # 只保留前 max_concurrent 个 task 调用，丢弃其余
        indices_to_drop = set(task_indices[self.max_concurrent:])
        truncated = [tc for i, tc in enumerate(tool_calls) if i not in indices_to_drop]

        # 替换 AIMessage（相同 id 触发 LangGraph 替换）
        updated_msg = last_msg.model_copy(update={"tool_calls": truncated})
        return {"messages": [updated_msg]}
```

**钳制范围**：`max_concurrent` 被 clamp 到 [2, 4]，默认 3

### 5.2 双重保障

| 层 | 机制 | 效果 |
|----|------|------|
| 提示词 | `<subagent_system>` 中的硬性约束说明 | 引导 LLM 不超限 |
| 中间件 | `SubagentLimitMiddleware` 截断 | LLM 无视说明时强制截断 |

---

## 六、完整调用流程

### 6.1 task_tool 执行

**文件**：`tools/builtins/task_tool.py`

```python
@tool("task", parse_docstring=True)
async def task_tool(runtime, description, prompt, subagent_type, tool_call_id, max_turns=None):
    # 1. 校验 subagent_type
    config = get_subagent_config(subagent_type)
    if config is None:
        return f"Error: Unknown subagent type '{subagent_type}'"

    # 2. bash 子代理权限检查
    if subagent_type == "bash" and not is_host_bash_allowed():
        return f"Error: {LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE}"

    # 3. 注入 skills 提示词
    skills_section = get_skills_prompt_section()
    if skills_section:
        overrides["system_prompt"] = config.system_prompt + "\n\n" + skills_section

    # 4. 从父 agent 继承运行时状态
    sandbox_state = runtime.state.get("sandbox")
    thread_data = runtime.state.get("thread_data")
    thread_id = runtime.context.get("thread_id")
    parent_model = runtime.config["metadata"].get("model_name")

    # 5. 加载工具（subagent_enabled=False 防递归）
    tools = get_available_tools(model_name=parent_model, subagent_enabled=False)

    # 6. 创建执行器
    executor = SubagentExecutor(config, tools, parent_model, sandbox_state, thread_data, thread_id, trace_id)

    # 7. 启动后台执行
    task_id = executor.execute_async(prompt, task_id=tool_call_id)

    # 8. 轮询等待结果（每 5s）
    while True:
        result = get_background_task_result(task_id)
        # 推送 task_running 进度事件
        # 检查终态：COMPLETED / FAILED / TIMED_OUT
        await asyncio.sleep(5)
```

### 6.2 SubagentExecutor 执行

**文件**：`subagents/executor.py`

```
execute_async(prompt, task_id)
  │
  ├─ 创建 SubagentResult（全局 dict 跟踪状态）
  │   _background_tasks[task_id] = result
  │
  └─ 提交到调度线程池 _scheduler_pool
      │
      └─ run_task()
          ├─ 标记 RUNNING
          └─ 提交到执行线程池 _execution_pool（带超时）
              │
              └─ execute(prompt, result_holder)
                  │
                  └─ asyncio.run(_aexecute(prompt, result_holder))
                      │
                      ├─ _create_agent()
                      │   ├─ model = create_chat_model(继承父模型, thinking_enabled=False)
                      │   ├─ middlewares = build_subagent_runtime_middlewares(lazy_init=True)
                      │   └─ create_agent(model, tools, middleware, prompt, ThreadState)
                      │
                      ├─ _build_initial_state(prompt)
                      │   ├─ messages: [HumanMessage(content=prompt)]
                      │   ├─ sandbox: 继承父 agent 的 sandbox_state
                      │   └─ thread_data: 继承父 agent 的 thread_data
                      │
                      └─ agent.astream(state, config, stream_mode="values")
                          ├─ 独立 ReAct 循环
                          ├─ 实时捕获 AI 消息 → result.ai_messages
                          └─ 提取最终 AIMessage → result.result
```

### 6.3 双线程池架构

```python
# 调度池：负责后台任务调度和编排（3 个线程）
_scheduler_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="subagent-scheduler-")

# 执行池：负责实际 subagent 执行（3 个线程，支持超时）
_execution_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="subagent-exec-")
```

为什么两层：
- 调度池提交任务到执行池时可以设置超时（`execution_future.result(timeout=...)`）
- 执行池中的 `asyncio.run()` 创建新事件循环，使异步工具（如 MCP）在线程池中可用
- 调度池和执行池独立，避免一个 subagent 阻塞影响调度

### 6.4 状态跟踪

```python
_background_tasks: dict[str, SubagentResult] = {}  # 全局 dict
_background_tasks_lock = threading.Lock()           # 线程安全

@dataclass
class SubagentResult:
    task_id: str
    trace_id: str          # 分布式追踪（关联父/子日志）
    status: SubagentStatus # PENDING → RUNNING → COMPLETED/FAILED/TIMED_OUT
    result: str | None     # 最终文本结果
    error: str | None      # 错误信息
    ai_messages: list[dict]  # 实时 AI 消息（供进度推送）
    started_at: datetime
    completed_at: datetime
```

### 6.5 进度推送

task_tool 通过 `get_stream_writer()` 推送 SSE 事件给前端：

```python
writer = get_stream_writer()

# 启动
writer({"type": "task_started", "task_id": task_id, "description": description})

# 运行中（每条新 AI 消息）
writer({"type": "task_running", "task_id": task_id, "message": ai_message, "message_index": i})

# 完成
writer({"type": "task_completed", "task_id": task_id, "result": result})

# 失败/超时
writer({"type": "task_failed", "task_id": task_id, "error": error})
writer({"type": "task_timed_out", "task_id": task_id, "error": error})
```

---

## 七、资源继承与隔离

### 7.1 从父 Agent 继承

| 资源 | 继承方式 | 用途 |
|------|----------|------|
| `model` | `parent_model` → `create_chat_model()` | 使用相同模型 |
| `sandbox_state` | `runtime.state["sandbox"]` | 共享沙箱环境 |
| `thread_data` | `runtime.state["thread_data"]` | 共享工作目录 |
| `thread_id` | `runtime.context["thread_id"]` | 沙箱操作需要 |
| `skills` | 注入到 subagent system_prompt | 技能可用 |
| `trace_id` | `runtime.config["metadata"]["trace_id"]` | 分布式追踪 |

### 7.2 隔离

| | Lead Agent | Subagent |
|---|-----------|----------|
| 对话上下文 | 完整对话历史 | 只有 task prompt（单条 HumanMessage） |
| Checkpointer | 自动保存完整状态 | 无 checkpointer（独立执行） |
| task 工具 | 有 | 无（禁止递归） |
| ask_clarification | 有 | 无（无法与用户交互） |
| 中间件 | 完整 14 个 | 精简版（build_subagent_runtime_middlewares） |

---

## 八、超时与清理

### 8.1 超时机制

三层超时保障：

```
第 1 层：执行池超时
  execution_future.result(timeout=config.timeout_seconds)  # 默认 900s
  → 超时后设置 TIMED_OUT，cancel future

第 2 层：task_tool 轮询超时
  max_poll_count = (config.timeout_seconds + 60) // 5  # 轮询次数上限
  → 超时后返回 "Task polling timed out"

第 3 层：提示词约束
  subagent 的 max_turns 限制 ReAct 循环轮次
  general-purpose: 50 轮
  bash: 30 轮
```

### 8.2 清理机制

```python
# 正常完成：task_tool 返回后立即清理
cleanup_background_task(task_id)

# 取消时：启动延迟清理协程
asyncio.create_task(cleanup_when_done())
  → 每 5s 检查一次，等后台任务到达终态后清理
  → 超过 max_cleanup_polls 后放弃

# 只清理终态任务（避免竞态）
if result.status in {COMPLETED, FAILED, TIMED_OUT}:
    del _background_tasks[task_id]
```

---

## 九、时序图

```
T+0s    Lead Agent LLM 返回 3 个并行 task 调用
T+0s    SubagentLimitMiddleware 检查 → 未超限 → 放行
T+0s    task_tool × 3 并行启动

T+0.1s  SubagentExecutor.execute_async() × 3
T+0.1s  调度线程池接收 3 个任务
T+0.2s  执行线程池中各创建独立 agent
T+0.2s  3 个 subagent 各自开始 ReAct 循环

T+0.3s  writer({"type": "task_started"}) × 3 → SSE 推前端
T+5.3s  writer({"type": "task_running", "message": ...}) → 进度推送
T+10s   writer({"type": "task_running"}) → 进度推送
  ...

T+30s   subagent-1 完成 → writer({"type": "task_completed"})
T+35s   subagent-2 完成 → writer({"type": "task_completed"})
T+40s   subagent-3 完成 → writer({"type": "task_completed"})

T+40s   3 个 task_tool 各返回结果
T+40s   Lead Agent LLM 收到 3 条 ToolMessage
T+40s   Lead Agent 综合结果 → 返回用户最终答案
```

---

> 本文档：Lead agent 通过 `task` 工具将复杂任务分解为并行子任务委派给 subagent。两种内置 subagent（general-purpose 和 bash）在独立上下文中执行，继承父 agent 的模型、沙箱和工作目录，但不能递归创建 subagent 或与用户交互。SubagentLimitMiddleware 强制限制并发数（默认 3，钳制 [2,4]）。双线程池架构（调度池 + 执行池）提供超时控制和异步执行。进度通过 SSE 实时推送前端
