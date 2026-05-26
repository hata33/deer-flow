# Q&A 08: 中间件钩子 `wrap_model_call`

> `wrap_model_call` 的执行时机是什么？它和 `before_model` 的区别和执行顺序是什么？

---

## 执行时机

`wrap_model_call` **包裹整个 LLM 调用过程**——可以在调用前、调用后、异常时执行逻辑：

```
wrap_model_call(request, handler):
    │
    ├── 前置处理（修改 request: tools, messages, config）
    │
    ├── response = handler(request)  ← 实际的 LLM 调用
    │
    ├── 后置处理（检查/修改 response）
    │
    └── 异常处理（捕获 LLM 错误，决定重试或返回错误消息）
```

---

## 与 `before_model` 的区别

| 维度 | `before_model` | `wrap_model_call` |
|------|----------------|-------------------|
| **执行位置** | LLM 调用前 | 包裹 LLM 调用 |
| **能做什么** | 修改 state（消息列表） | 修改 request（工具列表、config） |
| **能阻止调用吗** | 不能 | 可以（不调用 handler） |
| **能处理错误吗** | 不能 | 可以（try/except handler） |
| **能修改响应吗** | 不能 | 可以（修改 response） |
| **参数** | `(state, runtime)` | `(request, handler)` |

**执行顺序**:

```
before_model(state, runtime)          ← 修改 state
    ↓
wrap_model_call(request, handler)     ← 修改 request/response/处理错误
    ├── 前置处理
    ├── response = handler(request)   ← LLM 调用
    ├── 后置处理
    └── 异常处理
```

---

## 实现了 wrap_model_call 的中间件

### 1. DeferredToolFilterMiddleware

**文件**: `middlewares/deferred_tool_filter_middleware.py`

**职责**: 从 LLM 的工具列表中移除延迟工具，防止直接调用。

**处理逻辑**:

```python
def wrap_model_call(self, request, handler):
    # 前置：从 request.tools 中移除延迟工具
    filtered_tools = self._filter_tools(request.tools)
    request.tools = filtered_tools

    # 调用 LLM
    response = handler(request)

    # 后置：如果 LLM 仍然调用了延迟工具 → 返回错误 ToolMessage
    if has_deferred_tool_calls(response):
        return error_tool_messages(response)

    return response
```

**为什么用 wrap_model_call 而非 before_model**: 需要修改 `request.tools`（工具列表），这是 `before_model` 无法做到的。同时需要检查 LLM 是否仍然"违规"调用了延迟工具。

---

### 2. LLMErrorHandlingMiddleware

**文件**: `middlewares/llm_error_handling_middleware.py`

**职责**: LLM 调用错误重试和熔断器。

**处理逻辑**:

```python
def wrap_model_call(self, request, handler):
    # 前置：检查熔断器
    if self.circuit_breaker.is_open():
        return error_response("LLM service unavailable")

    try:
        response = handler(request)
        self.circuit_breaker.record_success()
        return response

    except RetryableError as e:
        # 可重试错误（如 rate limit、timeout）
        for attempt in range(self.max_retries):
            try:
                response = handler(request)
                self.circuit_breaker.record_success()
                return response
            except RetryableError:
                continue

        # 重试耗尽 → 返回错误消息（不终止运行）
        self.circuit_breaker.record_failure()
        return error_response(str(e))

    except Exception as e:
        # 不可重试错误 → 直接返回错误消息
        self.circuit_breaker.record_failure()
        return error_response(str(e))
```

**为什么用 wrap_model_call**: 需要捕获 LLM 调用异常——`before_model` 在调用前执行，无法捕获调用时的错误。

---

### 3. DanglingToolCallMiddleware

**文件**: `middlewares/dangling_tool_call_middleware.py`

**职责**: 修补用户中断导致的悬挂工具调用。

**问题场景**:

```
上一轮:
  AIMessage(tool_calls=[{id:"tc-1", name:"bash"}])
  ← 用户中断，ToolMessage(tc-1) 未生成

当前轮:
  AIMessage(tool_calls=[{id:"tc-1", name:"bash"}])  ← 重新出现
  ← 但 LangGraph 期望先有 tc-1 的 ToolMessage
```

**处理逻辑**:

```python
def wrap_model_call(self, request, handler):
    # 前置：检查并修补消息序列
    patched_messages = self._build_patched_messages(request.messages)
    if patched_messages:
        request.messages = patched_messages

    return handler(request)
```

**修补策略**: 对于每个悬挂的 tool_call，插入一条"被用户中断"的 ToolMessage，让 LangGraph 的消息配对校验通过。

**为什么用 wrap_model_call 而非 before_model**: 需要修改 `request.messages`（而非 state.messages），并且需要在紧邻 LLM 调用前执行修补——`before_model` 修改的是 state，而 `wrap_model_call` 修改的是实际传给 LLM 的 request。

---

### 4. TodoMiddleware

**文件**: `middlewares/todo_middleware.py`

**职责**: 在 LLM 请求中增加任务完成提醒。

**处理逻辑**:

```python
def wrap_model_call(self, request, handler):
    # 增强 request，添加任务完成提醒
    augmented_request = self._augment_request(request)
    return handler(augmented_request)
```

**同时实现 `before_model` 和 `wrap_model_call` 的原因**:
- `before_model`: 注入 todo_reminder 到 state（对消息列表可见）
- `wrap_model_call`: 增强 LLM request（在系统提示词中强调任务完成条件）

---

## 执行顺序

中间件在 `wrap_model_call` 钩子中的执行顺序（外层→内层）：

```
DeferredToolFilterMiddleware        ← 最外层：过滤工具列表
    ↓
LLMErrorHandlingMiddleware          ← 熔断器 + 重试
    ↓
DanglingToolCallMiddleware          ← 修补消息序列
    ↓
TodoMiddleware                      ← 增强请求
    ↓
handler(request)                    ← 实际 LLM 调用
```

外层中间件首先处理，内层中间件最后处理。错误从内层向外层冒泡——`LLMErrorHandlingMiddleware` 可以捕获内层抛出的异常。

---

## 相关源码

| 中间件 | 文件 |
|--------|------|
| DeferredToolFilter | `backend/packages/harness/deerflow/agents/middlewares/deferred_tool_filter_middleware.py` |
| LLMErrorHandling | `backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py` |
| DanglingToolCall | `backend/packages/harness/deerflow/agents/middlewares/dangling_tool_call_middleware.py` |
| Todo | `backend/packages/harness/deerflow/agents/middlewares/todo_middleware.py` |

## 深入阅读

- [Agent 设计决策](../docs/core/agent/06-design-decisions.md)
- [Agent 实现分析](../docs/core/agent/07-implementation-analysis.md)
- [Agent 请求全流程](../docs/lifecycle/01-agent-request-flow.md)
