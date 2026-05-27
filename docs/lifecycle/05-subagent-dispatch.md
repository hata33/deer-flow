# 子代理派发全链路

> 从 `task` 工具调用到后台线程池执行、SSE 实时事件推送、Token 归属统计的完整跨模块协作路径。

---

## 全链路架构图

```
┌──────────┐  tool call   ┌─────────────────┐  limit   ┌──────────────────────────┐
│ LLM      │ ──────────▸  │ SubagentLimit   │ ──────▸  │ task() Tool              │
│ Response │   "task"     │ Middleware      │  ≤3      │ (task_tool.py)           │
└──────────┘              └─────────────────┘          └────────────┬─────────────┘
                                                                      │
                                                     ┌────────────────┤
                                                     ▼                ▼
                                          ┌──────────────┐  ┌──────────────────┐
                                          │ Scheduler    │  │ StreamWriter     │
                                          │ Thread Pool  │  │ (SSE events)     │
                                          │ (3 workers)  │  │ task_started     │
                                          └──────┬───────┘  │ task_running     │
                                                 │          │ task_completed   │
                                                 ▼          └──────────────────┘
                                          ┌──────────────┐          │
                                          │ Execution    │          │ SSE
                                          │ Event Loop   │          ▼
                                          │ (persistent) │  ┌──────────────┐
                                          └──────┬───────┘  │ StreamBridge │
                                                 │          │ → Frontend   │
                                                 ▼          └──────────────┘
                                          ┌──────────────┐
                                          │ Subagent     │
                                          │ Agent.astream│
                                          └──────┬───────┘
                                                 │
                                    ┌────────────┼────────────┐
                                    ▼            ▼            ▼
                              ┌──────────┐ ┌──────────┐ ┌──────────────┐
                              │ Token    │ │ Result   │ │ TokenUsage   │
                              │ Collector│ │ Status   │ │ Middleware   │
                              │          │ │ (atomic) │ │ (attribution)│
                              └──────────┘ └──────────┘ └──────────────┘
```

---

## 阶段 ①：LLM 工具调用 — task 工具

**核心文件**: `packages/harness/deerflow/tools/builtins/task_tool.py` → `task()`

**工具定义**:
```python
@tool("task")
def task(
    description: str,           # 任务描述
    prompt: str,                # 给子代理的提示词
    subagent_type: str = "general-purpose",  # 子代理类型
) -> str:
```

**调用流程**:
1. LLM 在响应中生成 `task` 工具调用（包含描述、提示词、子代理类型）
2. 工具框架解析参数，调用 `task()` 函数
3. 验证 `subagent_type` 是否在注册表中存在
4. 检查 bash 类型是否被允许（受主代理工具白名单约束）
5. 合并父代理和子代理配置中的技能允许列表

**跨模块协作**:
- **task() ↔ SubagentRegistry**: 查找子代理类型配置
- **task() ↔ ToolPolicy**: 合并技能的 `allowed-tools` 允许列表

---

## 阶段 ②：并发限制 — SubagentLimitMiddleware

**核心文件**: `packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py`

**拦截点**: `after_model()`（`aafter_model()` 为其异步镜像，逻辑相同）

**限制逻辑**:
```
MAX_CONCURRENT_SUBAGENTS = 3

LLM 返回的 AIMessage:
  tool_calls: [task-1, task-2, task-3, task-4]
                                ↓ 截断
  tool_calls: [task-1, task-2, task-3]    ← 只保留前 3 个
```

**实现方式**:
1. 在 `after_model()` 中检查 AIMessage 的 `tool_calls`
2. 统计 `task` 类型的工具调用数量
3. 如果超过 `MAX_CONCURRENT_SUBAGENTS`（默认 3），截断多余调用
4. 使用 `clone_ai_message_with_tool_calls()` 创建新的 AIMessage 替换原始消息

**设计决策**:
- 截断而非拒绝：保留前 N 个调用继续执行，避免完全失败
- 限制在 `after_model` 而非 `wrap_tool_call`：提前在模型输出阶段处理，避免浪费

**跨模块协作**:
- **SubagentLimitMiddleware ↔ Agent Factory**: 在 `make_lead_agent()` 中条件注册（仅当 `subagent_enabled=True`）
- **SubagentLimitMiddleware ↔ task_tool.py**: 被截断的调用不会到达 task 工具

---

## 阶段 ③：后台执行引擎 — SubagentExecutor

**核心文件**: `packages/harness/deerflow/subagents/executor.py`

**线程池架构**:
```
┌─────────────────────────────────────────────┐
│              _scheduler_pool (3 threads)     │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐     │
│  │Worker 1 │  │Worker 2 │  │Worker 3 │     │
│  └────┬────┘  └────┬────┘  └────┬────┘     │
│       │            │            │           │
│       └────────────┼────────────┘           │
│                    ▼                        │
│           ┌────────────────┐                │
│           │ Persistent     │                │
│           │ Event Loop     │                │
│           │ (asyncio)      │                │
│           └────────┬───────┘                │
│                    │                        │
│                    ▼                        │
│           ┌────────────────┐                │
│           │ agent.astream()│                │
│           └────────────────┘                │
└─────────────────────────────────────────────┘
```

**执行状态机**:
```
PENDING → RUNNING → COMPLETED
                  → FAILED
                  → TIMED_OUT
                  → CANCELLED
```

**状态管理**:
- `SubagentResult` 使用线程安全的终端状态转换
- 一旦进入终端状态（COMPLETED/FAILED/TIMED_OUT/CANCELLED），不可再变更

**执行流程**:
1. `execute_async()`: 创建 PENDING 状态的 `SubagentResult`，提交到 `_scheduler_pool`
2. `run_task()`: 更新状态为 RUNNING，在持久化事件循环中提交执行
3. `_aexecute()`: 构建状态、创建子代理、运行 `agent.astream()`
4. 收集 token 使用数据到 `SubagentTokenCollector`
5. 实时更新 `SubagentResult.ai_messages`（进度跟踪）
6. 超时处理：`FuturesTimeoutError` + `cancel_event` 信号
7. 设置终端状态

**超时机制**:
- 默认超时: 15 分钟
- 通过 `cancel_event` 通知协程停止
- 超时后状态设置为 `TIMED_OUT`

**跨模块协作**:
- **SubagentExecutor ↔ SubagentRegistry**: 获取子代理配置构建子代理
- **SubagentExecutor ↔ ModelFactory**: 为子代理创建独立的 LLM 实例
- **SubagentExecutor ↔ ToolRegistry**: 获取子代理的工具集（排除 `task` 工具防止递归）

---

## 阶段 ④：子代理注册表 — Registry

**核心文件**: `packages/harness/deerflow/subagents/registry.py`

**内置代理**:

| 代理类型 | 工具范围 | 最大轮次 |
|---------|---------|---------|
| `general-purpose` | 全部工具（排除 task/ask_clarification/present_files） | 100 |
| `bash` | 仅沙箱工具（bash, ls, read_file, write_file, str_replace） | 60 |

**配置层级**:
1. 内置代理默认配置
2. `config.yaml` 中的自定义代理定义
3. `config.yaml` 中的按代理覆盖
4. 全局默认值

**自定义代理**:
```yaml
# config.yaml
subagents:
  enabled: true
  agents:
    - name: "research-agent"
      model: "gpt-4o"
      max_turns: 50
      tools:
        - web_search
        - web_fetch
```

**跨模块协作**:
- **Registry ↔ config.yaml**: 读取自定义代理配置
- **Registry ↔ task_tool.py**: 验证 `subagent_type` 是否有效

---

## 阶段 ⑤：SSE 实时事件推送 — StreamBridge

**核心文件**: `packages/harness/deerflow/runtime/stream_bridge/`

**事件类型**:

| 事件 | 触发时机 | 数据内容 |
|------|---------|---------|
| `task_started` | 子代理开始执行 | task_id, subagent_type, description |
| `task_running` | 子代理产生输出 | task_id, progress, ai_messages |
| `task_completed` | 子代理成功完成 | task_id, result, token_usage |
| `task_failed` | 子代理执行失败 | task_id, error |
| `task_timed_out` | 子代理超时 | task_id |
| `task_cancelled` | 子代理被取消 | task_id |

**推送流程**:
1. `task_tool.py` 获取 `get_stream_writer()` 的写入句柄
2. 在子代理执行的关键节点调用 `writer.write(event, data)`
3. `StreamBridge` 将事件发布到订阅队列
4. 客户端通过 `bridge.subscribe(run_id)` 接收事件
5. 心跳机制（15 秒间隔）保持连接活跃

**轮询机制**:
```
task_tool.py 每 5 秒轮询:
  ┌──────────┐    get_background_task_result()    ┌───────────────┐
  │ task()   │ ─────────────────────────────────▸ │ SubagentResult│
  │ (主线程)  │ ◂────────────────────────────────── │ (后台线程)     │
  └──────────┘    返回当前状态和进度                └───────────────┘
```

**跨模块协作**:
- **StreamBridge ↔ Gateway API**: SSE 端点 `POST /api/threads/{id}/runs/stream`
- **StreamBridge ↔ Frontend**: Next.js EventSource 接收事件

---

## 阶段 ⑥：Lead Agent 汇总判断 — ReAct 回路

`task_tool.py` 返回的不是最终答案，而是一个 `ToolMessage`，内容为子代理的执行结果摘要。这个消息回到 LangGraph 的 ReAct 循环，**lead agent 再次进入 LLM 调用**，看到子代理结果后自主判断下一步。

**返回格式**（`task_tool.py:447-474`）:

| 子代理终态 | ToolMessage 内容 |
|-----------|-----------------|
| COMPLETED | `"Task Succeeded. Result: {result.result}"` |
| FAILED | `"Task failed. Error: {result.error}"` |
| TIMED_OUT | `"Task timed out. Error: {result.error}"` |
| CANCELLED | `"Task cancelled by user."` |

**Lead Agent 的三种判断**:

```
子代理返回 ToolMessage
    │
    ▼ Lead Agent 再次进入 LLM 调用（before_model → LLM → after_model）
    │
    ├─ 结果满意 → AIMessage(content="根据分析结果...")  无 tool_calls → 最终回复用户
    │
    ├─ 结果不够 → AIMessage(tool_calls=[{name: "task", ...}])  再次派发子代理
    │              （换 prompt、换 subagent_type、缩小范围等）
    │
    └─ 需综合多个子代理结果 → AIMessage(content="综合三个子代理的分析...")
                               （可能同时看到多个 ToolMessage，汇总后输出）
```

**完整回路**:

```
用户消息
  → Lead Agent LLM 推理
    → AIMessage(tool_calls=[task("数据分析", ...)])
      → SubagentExecutor 执行
        → ToolMessage("Task Succeeded. Result: 数据包含12个异常值...")
          → Lead Agent LLM 再次推理（看到子代理结果）
            → 判断: 结果不够详细
            → AIMessage(tool_calls=[task("异常值详细分析", ...)])
              → SubagentExecutor 再次执行
                → ToolMessage("Task Succeeded. Result: 异常值集中在...")
                  → Lead Agent LLM 第三次推理
                    → 判断: 信息充分
                    → AIMessage(content="分析完成。数据共12个异常值，集中在...") → 回复用户
```

这个回路是 LangGraph ReAct 图的标准行为：只要 AIMessage 包含 `tool_calls`，图就继续循环（`tool_calls → 工具执行 → ToolMessage → 再次 LLM`），直到 LLM 返回不含 `tool_calls` 的 AIMessage 才结束。

**跨模块协作**:
- **task_tool → LangGraph ReAct**: ToolMessage 回到消息列表，触发下一轮 LLM 调用
- **Lead Agent ↔ Checkpointer**: 每轮循环自动保存 checkpoint，支持中断恢复
- **Lead Agent ↔ 中间件链**: 每轮循环都经过完整的中间件链（SummarizationMiddleware 可能压缩上下文、LoopDetectionMiddleware 检测循环等）

---

## 阶段 ⑥：Token 归属 — TokenUsageMiddleware

**核心文件**: `packages/harness/deerflow/agents/middlewares/token_usage_middleware.py`

**Token 收集**:
```
packages/harness/deerflow/subagents/token_collector.py
  → SubagentTokenCollector
  → on_llm_end() 捕获 usage_metadata
  → run_id 去重
  → 存入 SubagentResult.token_usage_records
```

**归属逻辑**:
1. `TokenUsageMiddleware` 在 `after_tool` 阶段检测 `task` 工具完成
2. 从 `ToolMessage` 中提取子代理 token 使用数据
3. 反向查找对应的 `AIMessage`（通过 `tool_call_id` 匹配）
4. 将子代理 token 合并到父代理 `AIMessage` 的 `usage_metadata`
5. 构建归属元数据供前端展示

**缓存机制**:
- `_subagent_usage_cache`: 按 `tool_call_id` 缓存子代理使用量
- 仅在 token 追踪启用时激活
- 合并回父消息时使用消息位置而非消息 ID（更可靠）

**跨模块协作**:
- **TokenUsageMiddleware ↔ task_tool.py**: 通过 `_subagent_usage_cache` 共享数据
- **TokenUsageMiddleware ↔ SubagentExecutor**: 读取 `token_usage_records`
- **TokenUsageMiddleware ↔ Frontend**: 提供分层 token 用量展示

---

## 跨模块交互总览

```
LLM Response (task tool call)
    │
    ├──▸ SubagentLimitMiddleware ──── 截断超过 3 个的 task 调用
    │
    ▼
task() Tool (task_tool.py)
    │
    ├──▸ SubagentRegistry ──── 验证 subagent_type
    ├──▸ ToolPolicy ──── 合并 allowed-tools
    │
    ▼
SubagentExecutor.execute_async()
    │
    ├──▸ _scheduler_pool ──── 提交调度任务
    │       │
    │       ▼
    │   Persistent Event Loop
    │       │
    │       ├──▸ agent.astream() ──── 执行子代理
    │       ├──▸ TokenCollector ──── 收集 token
    │       └──▸ SubagentResult ──── 更新状态
    │
    ├──▸ StreamWriter ──── SSE 事件推送
    │       │
    │       ▼
    │   StreamBridge → Frontend
    │
    ├──▸ 轮询 (5s) ──── 检查 SubagentResult
    │
    ▼
TokenUsageMiddleware
    │
    ├──▸ _subagent_usage_cache ──── 读取子代理 token
    └──▸ AIMessage.usage_metadata ──── 合并归属
```

---

## 深入阅读

| 模块内文档 | 路径 |
|-----------|------|
| 子代理系统 | `docs/core/subagents/` |
| 中间件链 | `docs/core/agent/middlewares/` |
| Agent 请求全流程 | `docs/lifecycle/01-agent-request-flow.md` |
| 运行时系统 | `docs/core/runtime/` |
| 工具系统 | `docs/core/tools/` |
