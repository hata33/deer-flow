# 06 - Guardrails 实现机制深度分析

> 本文档基于 `backend/packages/harness/deerflow/guardrails/` 目录下的源码，逐层拆解 Guardrails 系统的实现细节。回答的是"代码怎么写的、为什么这么写"。

---

## 一、模块依赖关系图

```
┌──────────────────────────────────────────────────────────────────────┐
│                       调用方（外部世界）                              │
│                                                                       │
│  middlewares/tool_error_handling_middleware.py                        │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ _build_runtime_middlewares()                                   │  │
│  │   guardrails_config = app_config.guardrails                    │  │
│  │   if guardrails_config.enabled and guardrails_config.provider: │  │
│  │     provider_cls = resolve_variable(guardrails_config.provider.use) │
│  │     provider = provider_cls(**config)                          │  │
│  │     middlewares.append(GuardrailMiddleware(provider, ...))     │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────────┐
│                    guardrails 包（内部世界）                            │
│                                                                       │
│  __init__.py ─── 公开接口导出                                         │
│                                                                       │
│  ┌───────────────────┐  ┌───────────────────┐  ┌──────────────────┐ │
│  │ provider.py       │  │ middleware.py      │  │ builtin.py       │ │
│  │                   │  │                    │  │                  │ │
│  │ GuardrailRequest  │  │ GuardrailMiddleware│  │ AllowlistProvider│ │
│  │ GuardrailDecision │  │  wrap_tool_call()  │  │  evaluate()      │ │
│  │ GuardrailReason   │  │  awrap_tool_call() │  │  aevaluate()     │ │
│  │ GuardrailProvider │  │                    │  │                  │ │
│  │   (Protocol)      │  │                    │  │                  │ │
│  └───────────────────┘  └───────────────────┘  └──────────────────┘ │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ 配置层                                                       │   │
│  │ config/guardrails_config.py → GuardrailsConfig               │   │
│  │   enabled, fail_closed, passport, provider.use, provider.config│  │
│  └──────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 二、第 1 层：GuardrailMiddleware 工作流程

### 2.1 完整拦截流程

```
LLM 生成 AIMessage(tool_calls=[{name: "bash", args: {command: "rm -rf /"}}])
  │
  ▼
ToolNode 分发工具调用
  │
  ▼
GuardrailMiddleware.wrap_tool_call(request, handler)
  │
  ├─ ① 构建评估请求
  │    gr = _build_request(request)
  │    GuardrailRequest(
  │      tool_name="bash",
  │      tool_input={"command": "rm -rf /"},
  │      agent_id=self.passport,        # 来自 guardrails_config.passport
  │      timestamp="2026-05-26T12:00:00+00:00"
  │    )
  │
  ├─ ② 调用 Provider 评估
  │    try:
  │      decision = provider.evaluate(gr)
  │    except GraphBubbleUp:
  │      raise  ← LangGraph 控制流透传
  │    except Exception:
  │      if fail_closed:
  │        decision = GuardrailDecision(allow=False,
  │          reasons=[GuardrailReason(code="oap.evaluator_error")])
  │      else:
  │        return handler(request)  ← 放行
  │
  ├─ ③a 允许 → 放行
  │    return handler(request)  ← 执行实际工具
  │
  └─ ③b 拒绝 → 构建错误消息
       return ToolMessage(
         content="Guardrail denied: tool 'bash' was blocked (oap.tool_not_allowed).
                  Reason: tool 'bash' is denied. Choose an alternative approach.",
         tool_call_id=...,
         name="bash",
         status="error"
       )
```

### 2.2 _build_request 实现

```python
def _build_request(self, request: ToolCallRequest) -> GuardrailRequest:
    return GuardrailRequest(
        tool_name=str(request.tool_call.get("name", "")),    # 工具名
        tool_input=request.tool_call.get("args", {}),         # 工具参数
        agent_id=self.passport,                               # 护照路径
        timestamp=datetime.now(UTC).isoformat(),              # ISO 8601 时间戳
    )
```

**为什么 thread_id 和 is_subagent 保留默认值**：当前版本的 Provider（AllowlistProvider）不需要这些字段。它们预留给高级 Provider 实现——如基于会话上下文的策略或对子代理施加更严格限制。

### 2.3 _build_denied_message 实现

```python
def _build_denied_message(self, request, decision) -> ToolMessage:
    reason_text = decision.reasons[0].message if decision.reasons else "blocked by guardrail policy"
    reason_code = decision.reasons[0].code if decision.reasons else "oap.denied"
    return ToolMessage(
        content=f"Guardrail denied: tool '{tool_name}' was blocked ({reason_code}). "
                f"Reason: {reason_text}. Choose an alternative approach.",
        tool_call_id=...,
        name=tool_name,
        status="error",
    )
```

**消息设计意图**：`status="error"` 告诉 Agent 这是一个工具调用失败（而非正常返回）。`reason_code` 帮助 Agent 理解具体被拒绝的原因。末尾的 "Choose an alternative approach" 引导 Agent 选择替代方案，而非重复尝试同一工具。

---

## 三、第 2 层：Provider 解析链

### 3.1 从配置到实例的完整路径

```
config.yaml:
  guardrails:
    enabled: true
    fail_closed: true
    passport: "/agents/my-agent"
    provider:
      use: "deerflow.guardrails.builtin:AllowlistProvider"
      config:
        allowed_tools: ["read_file", "web_search"]

                    ↓ _build_runtime_middlewares()

guardrails_config = app_config.guardrails
  │
  ├─ 检查 enabled=True
  ├─ 检查 provider 不为 None
  │
  ├─ resolve_variable(guardrails_config.provider.use)
  │   └─ importlib("deerflow.guardrails.builtin")
  │      .getattr("AllowlistProvider") → <class AllowlistProvider>
  │
  ├─ provider_kwargs = dict(guardrails_config.provider.config)
  │   └─ {"allowed_tools": ["read_file", "web_search"]}
  │
  ├─ framework 参数注入（如果 Provider 接受）
  │   sig = inspect.signature(provider_cls.__init__)
  │   if "framework" in sig.parameters:
  │     provider_kwargs["framework"] = "deerflow"
  │
  ├─ provider = AllowlistProvider(allowed_tools=["read_file", "web_search"])
  │
  └─ GuardrailMiddleware(provider, fail_closed=True, passport="/agents/my-agent")
```

### 3.2 framework 参数注入

某些第三方 Provider（如 OAP 策略 Provider）需要知道运行时框架才能发现配置。`_build_runtime_middlewares()` 通过 `inspect.signature()` 检查 Provider 的 `__init__` 是否接受 `framework` 参数。如果接受，自动注入 `"deerflow"` 值。内置的 `AllowlistProvider` 不需要此参数，`inspect` 检测后会跳过。

---

## 四、第 3 层：AllowlistProvider 实现

### 4.1 评估流程

```
AllowlistProvider.evaluate(request)
  │
  ├─ 白名单检查
  │   if self._allowed is not None and request.tool_name not in self._allowed:
  │     return GuardrailDecision(
  │       allow=False,
  │       reasons=[GuardrailReason(code="oap.tool_not_allowed",
  │                                message=f"tool '{name}' not in allowlist")]
  │     )
  │
  ├─ 黑名单检查
  │   if request.tool_name in self._denied:
  │     return GuardrailDecision(
  │       allow=False,
  │       reasons=[GuardrailReason(code="oap.tool_not_allowed",
  │                                message=f"tool '{name}' is denied")]
  │     )
  │
  └─ 默认允许
      return GuardrailDecision(
        allow=True,
        reasons=[GuardrailReason(code="oap.allowed")]
      )
```

### 4.2 set 存储设计

```python
def __init__(self, *, allowed_tools=None, denied_tools=None):
    self._allowed = set(allowed_tools) if allowed_tools else None  # None = 不启用白名单
    self._denied = set(denied_tools) if denied_tools else set()     # 空 set = 不启用黑名单
```

**None vs 空 set 的语义区别**：
- `_allowed = None`：未配置白名单，所有工具默认允许（仅检查黑名单）
- `_allowed = set()`：空白名单，所有工具都被拒绝（配置为 `allowed_tools: []` 的情况）

### 4.3 异步委托

```python
async def aevaluate(self, request) -> GuardrailDecision:
    return self.evaluate(request)  # 纯内存操作，无需异步
```

---

## 五、第 4 层：Decision 传播到 Agent

### 5.1 Agent 看到的错误消息

当工具被拒绝时，Agent 在后续 ReAct 步骤中看到：

```
AIMessage: [tool_call: bash(command="rm -rf /")]
ToolMessage(status="error"):
  "Guardrail denied: tool 'bash' was blocked (oap.tool_not_allowed).
   Reason: tool 'bash' is denied. Choose an alternative approach."
```

Agent 的 ReAct 循环继续运行。LLM 看到错误消息后，可以：
1. 选择替代工具（如用 `read_file` 替代 `bash`）
2. 向用户解释限制
3. 尝试不同的实现方案

### 5.2 中间件链中的位置

```
... → GuardrailMiddleware → SandboxAuditMiddleware → ToolErrorHandlingMiddleware → ...
       ↑ 第 4 位
```

**为什么在第 4 位**：
- 在沙箱初始化之后（工具调用上下文已完整构建）
- 在 ToolErrorHandling 之前（拒绝消息也被 ToolErrorHandling 兜底）
- 在业务中间件之前（Summarization/Memory 等不应看到被拒绝的工具调用）

如果 GuardrailMiddleware 自身的 `_build_denied_message()` 抛出异常，`ToolErrorHandlingMiddleware` 会捕获并转换为通用错误 ToolMessage，确保运行不会崩溃。

---

## 六、文件职责速查表

| 文件 | 核心职责 | 关键类/函数 |
|------|----------|------------|
| `__init__.py` | 公开接口导出 | `__all__` 列表 |
| `provider.py` | 数据类型 + Protocol 定义 | `GuardrailRequest`, `GuardrailDecision`, `GuardrailReason`, `GuardrailProvider` |
| `middleware.py` | 工具调用拦截中间件 | `GuardrailMiddleware`, `_build_request()`, `_build_denied_message()` |
| `builtin.py` | 内置白名单 Provider | `AllowlistProvider`, `evaluate()`, `aevaluate()` |

**外部依赖**：

| 文件 | 位置 | 职责 |
|------|------|------|
| `config/guardrails_config.py` | config/ | `GuardrailsConfig` Pydantic 模型 |
| `middlewares/tool_error_handling_middleware.py` | agents/middlewares/ | `_build_runtime_middlewares()` 注册 GuardrailMiddleware |
| `reflection/__init__.py` | reflection/ | `resolve_variable()` 反射加载 Provider 类 |
