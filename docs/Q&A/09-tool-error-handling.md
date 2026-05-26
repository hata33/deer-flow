# Q&A 09: 工具调用失败的处理

> 修补工具调用失败是在哪个环节处理的？具体的修补策略是什么？

---

## 处理环节：`wrap_tool_call` 钩子

工具调用失败在 **`ToolErrorHandlingMiddleware`** 中处理，位于 `wrap_tool_call` 钩子。

```
ReAct 循环：
    ...
    ├── wrap_model_call     ← LLM 调用
    ├── LLM 返回 tool_calls
    ├── wrap_tool_call      ← 在这里拦截工具执行
    │   ├── 执行工具
    │   └── 捕获异常 → 转为 ToolMessage
    └── after_model
```

---

## 核心策略：错误转 ToolMessage

**关键设计决策**: 工具调用失败**不会终止运行**，而是将错误转为 `ToolMessage`，让 Agent 自主决定下一步。

```python
# tool_error_handling_middleware.py — wrap_tool_call 钩子
def wrap_tool_call(self, tool_call, handler):
    try:
        return handler(tool_call)  # 正常执行工具
    except GraphBubbleUp:
        raise  # 保留 LangGraph 控制流信号
    except Exception as exc:
        # 错误转为 ToolMessage
        return ToolMessage(
            content=self._format_error(tool_call, exc),
            tool_call_id=tool_call.id,
            name=tool_call.name,
            status="error",
        )
```

---

## 错误消息格式

```python
def _format_error(self, tool_call, exc):
    tool_name = tool_call.name
    detail = str(exc)[:500]  # 截断到 500 字符
    return (
        f"Error: Tool '{tool_name}' failed with "
        f"{exc.__class__.__name__}: {detail}. "
        f"Continue with available context, or choose an alternative tool."
    )
```

**示例输出**:

```
Error: Tool 'bash' failed with TimeoutError: Command timed out after 30s.
Continue with available context, or choose an alternative tool.
```

---

## 为什么不终止运行

| 策略 | 终止运行 | 转为 ToolMessage |
|------|---------|-----------------|
| Agent 能否恢复 | 不能 | 能——Agent 可以选择替代方案 |
| 用户体验 | 对话中断 | 自然恢复 |
| 适用场景 | 致命错误 | 非致命错误（网络超时、文件不存在等） |

**Agent 的典型响应**:

```
ToolMessage: "Error: Tool 'bash' failed with TimeoutError..."

Agent 推理: "bash 执行超时了，让我用 read_file 直接读取文件内容"
→ 调用 read_file（替代方案）
```

---

## 异常分层

```
Exception
├── GraphBubbleUp        ← 直接 raise，保留 LangGraph 控制流
│   ├── GraphInterrupt   ← 中断信号（HITL）
│   └── GraphReturn      ← 提前返回
│
└── 其他所有异常          ← 转为 ToolMessage
    ├── TimeoutError
    ├── FileNotFoundError
    ├── PermissionError
    └── ...
```

**`GraphBubbleUp` 不被捕获的原因**: 这些是 LangGraph 的控制流信号（如 HITL 中断），必须在图层面处理。

---

## 与 DanglingToolCallMiddleware 的关系

`DanglingToolCallMiddleware` 处理的是**不同的问题**：

| 中间件 | 处理的问题 | 发生时机 |
|--------|-----------|---------|
| ToolErrorHandlingMiddleware | 工具执行时抛出异常 | `wrap_tool_call`（工具执行中） |
| DanglingToolCallMiddleware | 工具调用缺少配对的 ToolMessage | `wrap_model_call`（LLM 调用前） |

DanglingToolCall 是用户中断导致的——上一轮 Agent 产出了 tool_call，但用户中断导致 ToolMessage 未生成。这属于消息序列修补，而非错误处理。

---

## 相关源码

| 组件 | 文件 |
|------|------|
| 错误处理中间件 | `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py` |
| 悬挂工具调用中间件 | `backend/packages/harness/deerflow/agents/middlewares/dangling_tool_call_middleware.py` |

## 深入阅读

- [Agent 设计决策](../docs/core/agent/06-design-decisions.md)
- [Agent 请求全流程](../docs/lifecycle/01-agent-request-flow.md)
