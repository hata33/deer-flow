# Q&A 07: 中间件钩子 `before_model`

> `before_model` 钩子在调用 LLM 之前被触发，它的具体执行时机是什么？它负责处理哪些逻辑？

---

## 执行时机

`before_model` 在 LangGraph 的 ReAct 循环中，**每次 LLM 调用之前**被触发：

```
ReAct 循环：
    ┌──────────────────────────────────────────┐
    │  1. before_agent   ← Agent 运行开始       │
    │  2. before_model   ← 本次 LLM 调用前      │  ← 在这里
    │  3. wrap_model_call ← 包裹 LLM 调用       │
    │     ├── 前置处理                           │
    │     ├── LLM 调用                           │
    │     └── 后置处理                           │
    │  4. after_model    ← LLM 返回后            │
    │  5. wrap_tool_call ← 工具调用（如有）       │
    │  6. after_agent    ← Agent 运行结束         │
    └──────────────────────────────────────────┘
```

**重要**: 在一次 Agent 运行中，LLM 可能被调用**多次**（ReAct 循环），每次调用前都会触发 `before_model`。

---

## 实现了 before_model 的中间件

### 1. SummarizationMiddleware

**文件**: `middlewares/summarization_middleware.py`

**职责**: 当对话历史过长时，自动压缩早期消息。

**触发条件**: token 数量接近上限（通过 `summarization` 配置节控制阈值）。

**处理逻辑**:

```
before_model 执行：
    ├── 检查当前 token 使用量
    │
    ├── 超过阈值？
    │   ├── 否 → 不做任何处理
    │   └── 是 → 执行压缩：
    │       ├── 保留最近的技能调用（带 token 预算）
    │       ├── 保留 DynamicContext 注入的上下文
    │       ├── 对早期消息生成摘要
    │       ├── 用 RemoveMessage 删除原始消息
    │       └── 插入摘要 HumanMessage
    │
    └── 通过 SSE 通知前端（SummarizationMiddleware.before_model 事件）
```

**三阶段分区**:
1. **基础分区**: 确定可压缩范围（保留系统提示词 + 最近 N 条消息）
2. **技能救援**: 从可压缩区中找出技能调用（保留重要的技能上下文）
3. **Reminder 保护**: 不压缩 todo_reminder 消息

---

### 2. TodoMiddleware

**文件**: `middlewares/todo_middleware.py`

**职责**: 确保 Agent 持续跟踪待办任务。

**触发条件**: todo 列表存在但 `write_todos` 工具调用不在当前可见上下文中。

**处理逻辑**:

```python
def before_model(self, state, runtime):
    # 检查是否存在 todo 列表
    if not has_todos(state):
        return

    # 检查 write_todos 是否在可见上下文中
    if write_todos_in_context(state):
        return  # Agent 能看到 todo，不需要提醒

    # 注入 todo_reminder 消息
    reminder = HumanMessage(
        content=f"你的待办事项：\n{format_todos(state)}",
        name="todo_reminder",
    )
    state.messages.append(reminder)
```

**为什么需要**: 对话压缩后，`write_todos` 的工具调用可能被移除。注入 reminder 确保 Agent 不会"忘记"任务。

---

### 3. ViewImageMiddleware

**文件**: `middlewares/view_image_middleware.py`

**职责**: 在 LLM 调用前注入图像详情描述。

**触发条件**: 前一轮包含 `view_image` 工具调用且已完成。

**处理逻辑**:

```python
def before_model(self, state, runtime):
    # 检查上一轮是否有 view_image 工具调用
    last_tool_calls = get_last_tool_calls(state)
    view_image_calls = [tc for tc in last_tool_calls if tc.name == "view_image"]

    if not view_image_calls:
        return

    # 从工具结果中提取图像信息
    for call in view_image_calls:
        image_info = get_tool_result(state, call.id)
        # 将图像描述转换为文本注入
        description = format_image_description(image_info)
        state.messages.append(description)
```

**为什么在 before_model**: LLM 在当前轮次需要"看到"图像信息才能正确回答。非视觉模型无法直接处理图像，因此通过文本描述补充。

---

## 执行顺序

三个中间件按注册顺序执行：

```
1. SummarizationMiddleware  — 先压缩（可能减少消息数量）
2. TodoMiddleware            — 再注入 todo reminder（确保不被压缩掉）
3. ViewImageMiddleware       — 最后注入图像描述（基于压缩后的消息）
```

这个顺序有设计考量：压缩可能移除消息，reminder 需要在压缩后注入以确保不被移除。

---

## before_model 能做什么

`before_model` 可以**修改 state**（消息列表、metadata 等），但不能：
- 阻止 LLM 调用（那是 `wrap_model_call` 的能力）
- 处理 LLM 错误（那是 `wrap_model_call` 的能力）
- 直接修改 LLM 的请求参数（工具列表、temperature 等）

**典型用途**: 上下文准备 — 压缩、注入提醒、补充信息。

---

## 相关源码

| 中间件 | 文件 |
|--------|------|
| Summarization | `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py` |
| Todo | `backend/packages/harness/deerflow/agents/middlewares/todo_middleware.py` |
| ViewImage | `backend/packages/harness/deerflow/agents/middlewares/view_image_middleware.py` |
| 中间件基类 | LangGraph SDK `AgentMiddleware` |

## 深入阅读

- [Agent 设计决策](../docs/core/agent/06-design-decisions.md)
- [上下文压缩](../docs/lifecycle/02-context-compression.md)
- [Agent 请求全流程](../docs/lifecycle/01-agent-request-flow.md)
