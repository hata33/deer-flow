# Q&A 10: 截断超出限制的 Task 调用

> "截断超出限制的 task 调用"具体指什么？是什么限制触发了截断？截断策略是什么？

---

## 问题描述

当 LLM 一次产出**多个 `task` 工具调用**（子代理任务）时，系统会截断超额的调用。这是由 `SubagentLimitMiddleware` 实现的。

---

## 触发条件

LLM 在一次响应中可能产出任意数量的 `task` 工具调用：

```
AIMessage(tool_calls=[
    {id:"tc-1", name:"task", args:{description:"研究前端框架"}},
    {id:"tc-2", name:"task", args:{description:"设计数据库"}},
    {id:"tc-3", name:"task", args:{description:"编写测试"}},
    {id:"tc-4", name:"task", args:{description:"部署服务"}},    ← 超出限制
    {id:"tc-5", name:"task", args:{description:"写文档"}},      ← 超出限制
])
```

系统限制并行子代理数量为 **3**（`MAX_CONCURRENT_SUBAGENTS = 3`）。

---

## 截断策略

`SubagentLimitMiddleware` 在 `after_model` 钩子中执行截断：

```python
# subagent_limit_middleware.py
MAX_CONCURRENT_SUBAGENTS = 3

def after_model(self, state, runtime):
    last_message = state.messages[-1]
    if not isinstance(last_message, AIMessage):
        return

    tool_calls = last_message.tool_calls
    task_calls = [tc for tc in tool_calls if tc.name == "task"]

    if len(task_calls) <= MAX_CONCURRENT_SUBAGENTS:
        return  # 未超限，不处理

    # 截断：只保留前 max_concurrent 个 task 调用
    kept = task_calls[:MAX_CONCURRENT_SUBAGENTS]
    removed = task_calls[MAX_CONCURRENT_SUBAGENTS:]

    # 重建 tool_calls 列表
    other_calls = [tc for tc in tool_calls if tc.name != "task"]
    last_message.tool_calls = other_calls + kept

    # 为被移除的 task 生成 ToolMessage（告诉 LLM 为什么没执行）
    for removed_call in removed:
        state.messages.append(ToolMessage(
            content=f"Task '{removed_call.args['description']}' was deferred "
                    f"due to concurrency limit ({MAX_CONCURRENT_SUBAGENTS}). "
                    f"Please re-issue after current tasks complete.",
            tool_call_id=removed_call.id,
            status="error",
        ))
```

---

## 截断效果

**截断前**:

```
AIMessage(tool_calls=[
    task("研究前端框架"),      ← 保留
    task("设计数据库"),        ← 保留
    task("编写测试"),          ← 保留
    task("部署服务"),          ← 截断
    task("写文档"),            ← 截断
])
```

**截断后**:

```
AIMessage(tool_calls=[
    task("研究前端框架"),      ← 执行
    task("设计数据库"),        ← 执行
    task("编写测试"),          ← 执行
])
ToolMessage: "Task '部署服务' was deferred..."   ← 告知 LLM
ToolMessage: "Task '写文档' was deferred..."     ← 告知 LLM
```

**LLM 的后续行为**: 当看到 ToolMessage 说任务被延迟后，LLM 通常会在下一轮重新提交这些任务。

---

## 其他类型的截断

除了子代理截断，系统还有其他资源限制：

### 1. 上下文压缩（SummarizationMiddleware）

| 限制类型 | 触发条件 | 策略 |
|---------|---------|------|
| Token 数量 | 接近模型上限 | 压缩早期消息为摘要 |
| 消息数量 | 超过配置阈值 | 移除最早的消息 |
| 上下文比例 | 超过 max_tokens 的某个比例 | 按比例压缩 |

### 2. 工具调用频率（LoopDetectionMiddleware）

| 限制类型 | 警告阈值 | 强制阈值 | 策略 |
|---------|---------|---------|------|
| 相同工具调用集（hash） | 3 次 | 5 次 | 警告 → 强制停止 |
| 单工具类型频率 | 30 次 | 50 次 | 警告 → 强制停止 |

### 3. 递归深度（LangGraph 内置）

| 限制 | 默认值 | 含义 |
|------|-------|------|
| `recursion_limit` | 1000 | ReAct 循环最大迭代次数 |

前端提交时设置：

```typescript
config: {
    recursion_limit: 1000,
}
```

---

## 截断与循环检测的区别

| 维度 | SubagentLimitMiddleware | LoopDetectionMiddleware |
|------|------------------------|------------------------|
| **检测对象** | 单次响应中的 task 调用数量 | 多次响应中的重复调用模式 |
| **触发原因** | 并发资源限制 | 死循环防护 |
| **发生时机** | `after_model`（LLM 返回后） | `after_model`（LLM 返回后） |
| **策略** | 保留前 N 个，其余延迟 | 警告 → 强制移除所有 tool_calls |

---

## 相关源码

| 组件 | 文件 |
|------|------|
| 子代理限制中间件 | `backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py` |
| 子代理执行器 | `backend/packages/harness/deerflow/subagents/executor.py` |
| 循环检测中间件 | `backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py` |
| 上下文压缩中间件 | `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py` |

## 深入阅读

- [子代理设计决策](../docs/core/subagents/05-design-decisions.md)
- [上下文压缩](../docs/lifecycle/02-context-compression.md)
- [Agent 设计决策](../docs/core/agent/06-design-decisions.md)
