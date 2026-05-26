# 05 - 设计决策与考量

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

---

## 一、核心设计决策清单

| # | 决策 | 一句话动机 |
|---|------|-----------|
| 1 | **双线程池（scheduler + 持久化事件循环）** | 避免 asyncio.run() 与主循环的 httpx 连接池冲突 |
| 2 | **MAX_CONCURRENT_SUBAGENTS = 3** | 资源上限：LLM 并发、内存、token 成本可控 |
| 3 | **SubagentResult 线程安全终态转换** | 超时线程和执行线程竞态写入，需原子保证 |
| 4 | **15 分钟默认超时** | 覆盖复杂多步骤任务，但防止无限挂起 |
| 5 | **general-purpose 排除 task 工具** | 防止子代理嵌套委派导致递归失控 |
| 6 | **bash 子代理受限工具集** | 只保留沙箱操作工具，聚焦命令执行场景 |

---

## 二、逐决策分析

### 决策 1：双线程池 vs 纯 asyncio

**问题**：子代理的 `agent.astream()` 是异步调用，但触发方是同步的 `task()` 工具（在 LangGraph 的同步工具调用路径中）。而且主 Agent 已经在运行的事件循环中，不能再调用 `asyncio.run()`。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 双线程池 + 持久化事件循环（当前） | 复用 httpx 连接池；无循环冲突 | 额外线程开销；ContextVar 需手动传递 |
| 每次 asyncio.run() 创建新循环 | 简单 | httpx AsyncClient 跨循环复用导致 crash |
| 全部在主循环中调度 | 最干净 | 工具调用路径是同步的，无法直接 await |

**选择双线程池**：

- `_scheduler_pool`（3 工作线程）：接收 `execute_async()` 提交的任务，负责状态转换（PENDING → RUNNING）和编排。
- 持久化事件循环（`_isolated_subagent_loop`）：在独立守护线程中运行的长生命周期 `asyncio.AbstractEventLoop`，通过 `asyncio.run_coroutine_threadsafe()` 提交子代理协程。

```
execute_async(task, task_id)
  │
  ├─ SubagentResult(PENDING) → _background_tasks[task_id]
  │
  └─ _scheduler_pool.submit(run_task)
       │
       ├─ status = RUNNING
       │
       └─ _submit_to_isolated_loop_in_context(
            copy_context(),              ← 捕获 ContextVar 快照
            lambda: _aexecute(task, result_holder)
          )
          │
          └─ asyncio.run_coroutine_threadsafe(coro, persistent_loop)
               │
               └─ 在持久化循环中执行 agent.astream()
```

**为什么不用临时循环**：LangChain 通过 `@lru_cache` 全局缓存 httpx `AsyncClient`。如果在临时循环中运行 `model.ainvoke()`，循环关闭后连接池被销毁，但 `@lru_cache` 仍持有引用，下次使用导致 crash。

---

### 决策 2：MAX_CONCURRENT_SUBAGENTS = 3

**问题**：主 Agent 的 LLM 可能在一次响应中生成多个 `task()` 工具调用（并行委派），如果不做限制，所有子代理同时启动会耗尽 LLM API 配额和内存。

| 值 | 效果 |
|----|------|
| 1 | 完全串行，无并行收益 |
| 3（当前） | 适度并行，资源可控 |
| 10 | 高并行，但 LLM API 限流风险大 |

**实现**：`SubagentLimitMiddleware` 在 `after_model` 阶段检查 AIMessage 的 `tool_calls`，如果 `task` 类型超过 3 个，截断多余调用并发出警告日志。`_scheduler_pool` 的 `max_workers=3` 与此对应。

**可配置性**：`_scheduler_pool` 硬编码为 3，`MAX_CONCURRENT_SUBAGENTS` 为模块常量。当前不支持通过 config.yaml 调整，因为线程池大小在模块加载时确定。

---

### 决策 3：SubagentResult 线程安全终态转换

**问题**：超时线程（`Future.result(timeout=)` 到期后）和执行线程（`_aexecute()` 正常完成）可能同时操作同一个 `SubagentResult`。如果不做原子保证，状态和载荷字段可能被覆盖。

| 方案 | 优势 | 劣势 |
|------|------|------|
| `try_set_terminal()` + Lock（当前） | 第一个终态写入胜出，后续被拒绝 | 额外锁开销 |
| 无锁直接赋值 | 最快 | 竞态条件导致状态不一致 |
| 队列串行化 | 无竞态 | 增加复杂度 |

**选择 Lock 保护**：

```python
def try_set_terminal(self, status, *, result=None, error=None):
    if not status.is_terminal:
        raise ValueError(...)
    with self._state_lock:
        if self.status.is_terminal:       # 已是终态
            return False                  # 拒绝后续写入
        self.status = status              # 原子设置终态
        self.result = result
        self.error = error
        self.completed_at = datetime.now()
        return True
```

终态（COMPLETED/FAILED/TIMED_OUT/CANCELLED）一旦设置不可逆转。超时线程设置 TIMED_OUT 后，执行线程的 COMPLETED 写入被拒绝（返回 False），反之亦然。

---

### 决策 4：15 分钟默认超时

**问题**：子代理执行时间不可预测。太短导致正常任务被终止，太长导致资源被挂起的任务占用。

| 场景 | 典型耗时 |
|------|---------|
| 简单文件操作 | 10-30 秒 |
| 多步代码重构 | 2-5 分钟 |
| 复杂研究 + 编写 | 5-10 分钟 |
| 超长构建/测试 | 10-15 分钟 |

**选择 15 分钟（900 秒）**：覆盖绝大多数场景，同时通过 `config.yaml` 的 `subagents.agents.{name}.timeout_seconds` 支持 per-agent 覆盖。

超时实现：`Future.result(timeout=timeout_seconds)` 在 `_scheduler_pool` 线程中等待。超时后设置 `cancel_event` 通知协作式取消，并在 `_aexecute()` 的 `astream` 迭代边界检测。

---

### 决策 5：general-purpose 排除 task 工具

**问题**：子代理继承父代理的工具列表。如果不排除 `task` 工具，子代理可以再次委派给子代理的子代理，形成无限递归。

| 方案 | 优势 | 劣势 |
|------|------|------|
| `disallowed_tools=["task"]`（当前） | 简单直接，硬编码禁止 | 不支持有限的嵌套层级 |
| 动态深度计数 | 灵活 | 实现复杂，状态管理困难 |
| prompt 约束 | 不改工具集 | LLM 可能不遵守 |

**选择黑名单**：`SubagentConfig.disallowed_tools` 默认值为 `["task"]`，`_filter_tools()` 在创建子代理时从工具列表中移除。所有内置代理和自定义代理都默认排除 task，除非显式覆盖。

同时排除 `ask_clarification`（子代理应自主执行，不向用户提问）和 `present_files`（展示由父代理负责）。

---

### 决策 6：bash 子代理受限工具集

**问题**：bash 子代理专注于命令行操作，给予过多工具会分散注意力，增加 LLM 选择错误工具的概率。

| 代理 | 工具策略 | 工具列表 |
|------|---------|---------|
| general-purpose | 继承全部（排除黑名单） | bash, ls, read_file, write_file, str_replace, glob, grep, MCP 工具... |
| bash | 显式白名单 | bash, ls, read_file, write_file, str_replace |

**选择白名单**：`tools=["bash", "ls", "read_file", "write_file", "str_replace"]` 仅保留沙箱文件操作工具。不包含 glob/grep（可通过 bash 命令实现）、不包含 MCP 工具、不包含任何社区工具。

bash 子代理的 `max_turns=60`（高于 general-purpose 的 100），因为命令序列通常轮次多但每轮 token 少。

---

## 三、实现效果

| 效果 | 实现方式 |
|------|----------|
| **并行执行** | 主 Agent 可同时派发最多 3 个子代理 |
| **非阻塞** | execute_async() 立即返回 task_id，5s 轮询结果 |
| **超时安全** | try_set_terminal() 保证竞态条件下状态一致 |
| **递归防护** | disallowed_tasks=["task"] 阻止子代理嵌套 |
| **资源可控** | MAX_CONCURRENT_SUBAGENTS + 线程池大小限制并发 |
| **上下文传递** | copy_context() 保留 ContextVar（user_id、trace_id） |
| **可扩展** | config.yaml custom_agents 添加自定义子代理 |
