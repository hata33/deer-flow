# 子智能体系统心智模型

> 来源：`backend/packages/harness/deerflow/subagents/`（executor、registry）、`tools/builtins/task_tool.py`、`agents/middlewares/subagent_limit.py`

## 1. 双线程池调度——调度与执行分离，超时不阻塞调度器

`SubagentExecutor` 使用两个独立线程池：`_scheduler_pool`（3 worker）负责接收任务、更新状态、发送事件；`_execution_pool`（3 worker）负责实际运行 Agent 图。调度器把任务提交给执行池后立即返回，不等待执行完成。超时控制在执行池层面通过 `Future.result(timeout=seconds)` 实现——超时后调度器能正常清理状态，不会因为某个子智能体卡死而阻塞整个调度队列。

不要用一个线程池同时做调度和执行。如果调度和执行共享池，一个 15 分钟的长任务会占住 worker，后续任务排队等待，调度器无法发送状态更新。分离后，调度器始终响应，执行池按需扩展。类似的模式在 AIO 沙箱提供者中也存在——`_idle_checker_thread` 独立于主线程运行清理逻辑，不阻塞 `acquire/release` 路径。

## 2. 轮询式进度推送——tool 函数内的同步等待变为 SSE 事件流

`task_tool` 是一个同步 LangChain tool 函数，但子智能体在后台线程异步执行。tool 函数通过 5 秒间隔轮询 `_result_holder`，每轮检查状态并发送 SSE 事件（`task_started` → `task_running` → `task_completed/failed/timed_out`）。前端通过 SSE 实时看到子智能体的进展，而不是等到全部完成后才返回结果。`_result_holder` 是一个简单的可变字典，后台线程写入、轮询线程读取，通过 `threading.Event` 同步终态信号。

不要在 tool 函数中阻塞等待后台任务而不提供中间反馈。用户发起任务后如果 15 分钟无响应，体验极差。轮询模式虽然不是最高效的（相比 asyncio 原生协程），但兼容 LangChain 的同步 tool 接口——tool 函数必须返回字符串，不能 yield 中间结果。用可变容器做进程内通信，后台线程写、轮询线程读，是同步-异步边界上的经典桥接手法。

## 3. 工具隔离——递归防护 + 最小权限

子智能体的工具集经过两层过滤：首先 `SubagentExecutor` 从父 Agent 的工具中移除 `task` 工具（防止子智能体再委派子智能体，形成递归调用链）；其次按子智能体类型筛选——`general-purpose` 继承几乎所有工具（除 `task`、`ask_clarification`、`present_files`），`bash` 只获得沙箱工具（`bash`、`ls`、`read_file`、`write_file`、`str_replace`）。`bash` 子智能体还需要安全检查：本地沙箱且未启用 `allow_host_bash` 时不可用。

不要给子智能体和父智能体完全相同的工具集。递归调用会导致不可控的调用深度和资源消耗——一个任务可以派生 3 个子任务，每个再派生 3 个，指数爆炸。最小权限原则：每个子智能体类型只获得完成其职责所需的工具。`bash` 只需要文件和命令操作，不需要 `present_files`（展示给用户是 lead agent 的事）。这和操作系统的 capability-based security 是同一思路。

## 4. 三重并发约束——与 [[003-prompt]] 的纵深防御一脉相承

子智能体并发控制有三层防护，与系统提示词的三重约束是同一模式：

- **第一层：提示词硬约束**（`_build_subagent_section`）——在 `<subagent_system>` 中反复强调 `MAXIMUM {n} task CALLS PER RESPONSE`，用 `⛔`、`HARD ERROR` 等强语气
- **第二层：思维引导**（`subagent_thinking`）——在 `<thinking_style>` 段落引导 LLM 在规划阶段就做计数和分批
- **第三层：中间件截断**（`SubagentLimitMiddleware`）——`after_model` 阶段物理截断超出的 `task` 工具调用，LLM 无法绕过

`MAX_CONCURRENT_SUBAGENTS` 默认为 3，被钳位到 [2, 4] 范围。中间件直接修改 `AIMessage.tool_calls`，删除超出的调用项，然后才传递给工具执行层。

不要只靠提示词约束 LLM 的行为边界。LLM 是概率模型，不是确定性程序——提示词说"最多 3 个"，它可能输出 5 个。三重纵深是 Agent 系统中"软约束 + 引导 + 硬约束"的标准模式。每一层独立生效，即使前两层全部失败，第三层仍能兜底。

## 5. 配置驱动的超时层级——全局默认 + 按类型覆盖

超时配置形成两级层次：`subagents.timeout_seconds`（全局默认 900 秒）+ `subagents.agents.{type}.timeout_seconds`（按子智能体类型覆盖）。`general-purpose` 默认 50 轮、`bash` 默认 30 轮。`task_tool` 从配置中读取超时值传给 `SubagentExecutor`，执行池通过 `Future.result(timeout=seconds)` 强制终止超时任务，发送 `task_timed_out` 事件。

不要对所有子智能体使用相同的超时。`bash` 子智能体执行命令序列，通常 5 分钟内完成；`general-purpose` 可能需要做多步研究、文件编辑，15 分钟以上很常见。统一的超时值要么让简单任务等太久，要么让复杂任务被过早终止。按类型配置超时，让调度器对不同性质的工作负载施加合理的资源限制。

## 6. 上下文继承——沙箱、线程数据、追踪 ID 自动传递

子智能体自动继承父 Agent 的运行时上下文：`sandbox_id`（沙箱隔离环境）、`thread_data`（线程目录信息）、trace ID（分布式追踪）。继承通过 `task_tool` 从父 Agent 的 state 中提取，注入到子智能体的 `config.configurable` 中。子智能体写入的文件出现在同一个线程的 `workspace/` 目录，父 Agent 可以直接读取——不需要跨沙箱的文件传输。

不要为子智能体创建独立的沙箱或线程上下文。子智能体是父 Agent 的"手"——它操作的文件系统应该和父 Agent 共享，否则父 Agent 看不到子智能体的产出。共享沙箱 + 共享线程目录意味着所有文件操作都发生在同一个 `/mnt/user-data/workspace/` 下，父 Agent 可以无缝接续子智能体的工作。trace ID 传递确保在分布式追踪系统中能看到完整的调用链。
