# SubAgent 子代理系统——底层逻辑与本质

## 一句话本质

SubAgent = **任务拆解 + 并行委派 + 结果综合**。主 Agent 识别可并行的子任务，通过 `task` 工具分发给独立子 Agent 执行，每个子 Agent 拥有独立上下文、沙箱状态和工具子集。

---

## 1. 子 Agent 不是"另一个对话"——它是"主 Agent 的手"

```
主 Agent（任务编排者）
  │
  ├─ task("财务数据分析", subagent_type="general-purpose")
  │     └─ 子 Agent A（独立上下文，继承沙箱）
  │           ├─ bash("python analysis.py")
  │           ├─ read_file("data.csv")
  │           └─ 返回分析结果
  │
  ├─ task("竞品新闻搜索", subagent_type="general-purpose")
  │     └─ 子 Agent B（并行执行）
  │           ├─ web_search("竞品 最新动态")
  │           └─ 返回搜索结果
  │
  └─ 综合结果 → 返回给用户
```

**子 Agent 与主 Agent 的关系不是"嵌套对话"，而是"任务外包"**：
- 子 Agent 看不到主 Agent 的对话历史，只收到 `prompt` 参数中的任务描述
- 子 Agent 继承主 Agent 的沙箱（`sandbox_id`）和线程目录（`thread_data`），文件操作发生在同一文件系统
- 子 Agent 不能再委派子 Agent（`task` 工具被 `disallowed_tools` 排除）

**核心启示**：子 Agent 的设计本质是"有限委托"——给足够完成任务的工具和上下文，但不给递归委派的能力。这避免了指数级调用爆炸：如果每个子 Agent 可以再派 3 个，3 层就是 27 个并发 Agent。一刀切断递归，整个系统的复杂度上限是 O(N)，N = `MAX_CONCURRENT_SUBAGENTS`。

## 2. 双线程池——调度与执行分离

```
_scheduler_pool (3 workers)          _execution_pool (3 workers)
  │                                    │
  ├─ 接收 task_tool 调用               ├─ 运行 Agent 图（astream）
  ├─ 提交执行任务到 execution_pool     ├─ 15 分钟超时控制
  ├─ 更新任务状态                      ├─ 收集 AI 消息流
  ├─ 发送 SSE 事件                     │
  │                                    │
  └─ 不等待执行完成，立即返回           └─ 结果写入共享 result_holder
```

**为什么需要两个池？** 如果调度和执行共享池，一个 15 分钟的长任务会占住 worker，后续任务排队等待，调度器无法发送状态更新。分离后，调度器始终响应（轻量操作：更新状态、发事件），执行池按需工作（重型操作：运行 LLM + 工具调用）。

**核心启示**：在"快速响应"和"重型计算"之间做资源隔离。调度器是面向用户的——它需要在 5 秒内返回进度更新；执行器是面向任务的——它可能运行 15 分钟。用不同的线程池为不同优先级的工作服务。

## 3. 轮询桥接——同步工具接口与异步执行之间的缝隙

`task_tool` 是一个 `async` LangChain tool 函数，但子 Agent 在后台线程执行。tool 函数通过 5 秒间隔轮询 `result_holder`：

```python
while True:
    result = get_background_task_result(task_id)

    # 检测新 AI 消息 → 发送 SSE 进度事件
    if len(result.ai_messages) > last_message_count:
        writer({"type": "task_running", "message": ...})

    # 检查终态
    if result.status == COMPLETED:
        return f"Task Succeeded. Result: {result.result}"

    await asyncio.sleep(5)  # 5 秒轮询间隔
```

**为什么用轮询而不是 asyncio 原生协程？** 因为子 Agent 的执行在 `_execution_pool`（线程池）中，不在 asyncio 事件循环中。线程和协程之间没有原生的通知机制。轮询通过可变容器（`result_holder`）做进程内通信——后台线程写入、轮询协程读取。

**核心启示**：同步/异步边界是 Agent 系统的永恒难题。LangChain 的工具接口是同步的（函数必须返回字符串），子 Agent 的执行是异步的（流式 AI 消息）。轮询虽然不是最高效的，但它兼容 LangChain 的接口约束，同时通过 SSE 给前端实时反馈。用可变容器做跨线程通信是经典桥接手法。

## 4. 三重并发约束——软约束 + 引导 + 硬约束

```
第一层：提示词硬约束
  "⛔ HARD CONCURRENCY LIMIT: MAXIMUM 3 task CALLS PER RESPONSE.
   THIS IS NOT OPTIONAL. Excess calls are silently discarded."

第二层：思维引导
  "DECOMPOSITION CHECK: Can this task be broken into 2+ parallel
   sub-tasks? If YES, COUNT them. If count > 3, you MUST plan
   batches of ≤3."

第三层：中间件截断（SubagentLimitMiddleware）
  after_model → 统计 AIMessage 中的 task 工具调用数
  → 超过 3 个则物理截断 tool_calls 列表
  → LLM 无法绕过
```

**为什么需要三层？** LLM 是概率模型，不是确定性程序。提示词说"最多 3 个"，它可能输出 5 个。只用提示词约束，成功率可能 90%，剩下 10% 就是资源爆炸。三重纵深让每一层独立生效，即使前两层全部失败，第三层仍能兜底。

**核心启示**：Agent 系统中"约束 LLM 行为"不能只靠提示词。硬约束必须在代码层面实施——中间件直接修改 `AIMessage.tool_calls`，删除超出的调用项。LLM 感知不到这个修改，它只看到自己的调用被执行了，不知道超出部分被静默丢弃。

## 5. 上下文继承——子 Agent 与主 Agent 共享工作空间

```python
# task_tool 中的上下文传递
sandbox_state = runtime.state.get("sandbox")      # 沙箱 ID
thread_data = runtime.state.get("thread_data")     # 线程目录
parent_model = metadata.get("model_name")          # 模型名称
trace_id = metadata.get("trace_id")                # 追踪 ID

executor = SubagentExecutor(
    sandbox_state=sandbox_state,    # 子 Agent 操作同一个沙箱
    thread_data=thread_data,        # 子 Agent 写入同一个线程目录
    parent_model=parent_model,      # 子 Agent 使用同一个模型
)
```

子 Agent 写入的文件出现在同一个线程的 `/mnt/user-data/workspace/` 目录下，主 Agent 可以直接用 `read_file` 读取——不需要跨沙箱的文件传输。

**核心启示**：子 Agent 是主 Agent 的"手"——它操作的文件系统应该和主 Agent 共享。如果子 Agent 有独立沙箱，主 Agent 看不到子 Agent 的产出，需要额外的文件传输机制。共享沙箱 + 共享线程目录 = 所有文件操作发生在同一个命名空间，主 Agent 可以无缝接续子 Agent 的工作。
