# 子代理系统全局概览

子代理系统是 DeerFlow Lead Agent（主代理）的任务委派机制。当主代理面对复杂、多步骤或需要特定专业知识的任务时，可以通过 `task()` 工具将子任务委派给专用子代理执行，从而实现关注点分离、上下文隔离和并行处理。

## 系统定位

子代理系统位于 Lead Agent 与底层工具之间，是任务委派的唯一通道：

```
Lead Agent (主代理)
    │
    │ task(description, subagent_type)
    ▼
task_tool (tools/builtins/task.py)
    │
    ├── SubagentExecutor.execute_async()  ← 后台异步执行
    │       │
    │       ▼
    │   _scheduler_pool (ThreadPoolExecutor, 3 workers)
    │       │
    │       ▼
    │   持久化事件循环 (isolated event loop)
    │       │
    │       ▼
    │   LangChain Agent 实例 (子代理)
    │       │
    │       ▼
    │   工具执行 (bash / ls / read_file / write_file / ...)
    │
    ├── 轮询 get_background_task_result() (5秒间隔)
    │       │
    │       ▼
    │   SSE 事件发射 (StreamWriter)
    │       │
    │       ▼
    │   前端实时更新
    │
    └── SubagentResult (终态结果 + token 用量)
```

## 并发模型

子代理系统采用双线程池架构：

| 组件 | 工作线程数 | 用途 |
|------|-----------|------|
| `_scheduler_pool` | 3 | 后台任务调度编排，提交协程到持久化事件循环 |
| 持久化事件循环 | 1（守护线程） | 执行子代理的异步 LangChain Agent 运行 |
| `MAX_CONCURRENT_SUBAGENTS` | 3 | 由 SubagentLimitMiddleware 强制执行的最大并发数 |

### 并发限制机制

`SubagentLimitMiddleware` 在 `after_model` 阶段检查主代理 LLM 响应中的 `task` 工具调用数量。当超过 `MAX_CONCURRENT_SUBAGENTS = 3` 时，中间件截断多余的调用并发出警告日志。这确保了：

- 同时运行的子代理不超过 3 个
- 系统资源（线程池、LLM API 并发、内存）不会被过度消耗
- 前端能合理展示并行任务进度

## 事件模型

子代理执行过程中的 SSE 事件流：

```
task_started       → 子代理已提交，等待执行
    │
    ▼
task_running        → 子代理正在执行（5 秒轮询间隔）
    │
    ├── task_completed    → 正常完成（包含结果消息）
    ├── task_failed       → 执行异常（包含错误消息）
    └── task_timed_out    → 超时（默认 15 分钟）
```

事件通过 `StreamWriter` 以 SSE（Server-Sent Events）格式推送给前端，实现实时任务进度展示。

## 内置代理

| 代理名称 | 用途 | 工具集 | max_turns |
|----------|------|--------|-----------|
| `general-purpose` | 复杂多步骤任务 | 继承全部工具（除 task） | 100 |
| `bash` | 命令执行专家 | bash, ls, read_file, write_file, str_replace | 60 |

两个内置代理都禁止使用以下工具：
- `task` — 防止子代理嵌套（无限递归风险）
- `ask_clarification` — 子代理应自主完成任务，不向用户提问
- `present_files` — 文件展示由主代理统一管理

## 自定义代理

用户可在 `config.yaml` 的 `subagents.custom_agents` 段中定义自定义代理：

```yaml
subagents:
  enabled: true
  custom_agents:
    code-reviewer:
      description: "Code review specialist"
      system_prompt: "You are a code reviewer..."
      tools: ["bash", "read_file", "ls"]
      disallowed_tools: ["task"]
      max_turns: 30
      timeout_seconds: 600
```

自定义代理与内置代理并列注册，由 `registry.py` 统一管理。

## 模块结构

```
subagents/
├── __init__.py              # 模块入口，导出公共 API
├── config.py                # SubagentConfig 数据类 + 模型解析
├── executor.py              # 执行引擎（双线程池 + 持久化事件循环）
├── registry.py              # 代理注册、发现、配置覆盖
├── token_collector.py       # LLM Token 用量收集回调
└── builtins/
    ├── __init__.py           # 内置代理注册表
    ├── bash_agent.py         # Bash 命令执行专家配置
    └── general_purpose.py    # 通用多步骤任务代理配置
```

## 超时与取消

| 机制 | 默认值 | 说明 |
|------|--------|------|
| 执行超时 | 900 秒（15 分钟） | 通过 `Future.result(timeout=)` 实现 |
| 轮询间隔 | 5 秒 | `task_tool` 轮询 `get_background_task_result()` 的间隔 |
| 协作式取消 | `threading.Event` | `cancel_event` 在 `astream` 迭代边界检查 |

超时处理流程：
1. `Future.result(timeout=)` 抛出 `FuturesTimeoutError`
2. 设置 `result_holder.cancel_event`
3. `try_set_terminal(TIMED_OUT)` 原子性地标记终态
4. `execution_future.cancel()` 尝试取消底层协程

## Token 用量追踪

每个子代理执行都会创建一个 `SubagentTokenCollector` 实例作为 LangChain 回调。它收集每次 LLM 调用的 token 用量（input_tokens, output_tokens, total_tokens），执行完成后存入 `SubagentResult.token_usage_records`，最终通过 `RunJournal.record_external_llm_usage_records()` 合并回父代理的用量统计。
