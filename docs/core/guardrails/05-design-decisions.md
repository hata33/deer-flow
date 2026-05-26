# 05 - Guardrails 设计决策与考量

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

---

## 一、核心设计决策清单

| # | 决策 | 一句话动机 |
|---|------|-----------|
| 1 | **Protocol-based GuardrailProvider vs ABC** | 结构化子类型，降低耦合，无需显式继承 |
| 2 | **fail_closed 默认策略** | 安全优先，Provider 异常时宁可误杀不可放过 |
| 3 | **evaluate 在 wrap_tool_call 中执行** | 在工具执行前拦截，已执行的调用无法撤回 |
| 4 | **AllowlistProvider 作为内置 Provider** | 零外部依赖，覆盖最常见的安全需求 |
| 5 | **OAP 标准对齐** | 与 Open Agent Passport 规范兼容，便于集成企业策略 |

---

## 二、逐决策分析

### 决策 1：Protocol-based GuardrailProvider vs ABC

**问题**：Guardrails 的 Provider 接口如何定义？

| 方案 | 优势 | 劣势 |
|------|------|------|
| `Protocol`（当前） | 任何拥有 `evaluate`/`aevaluate` 方法的类自动满足协议；无需导入 DeerFlow 类型 | 无强制方法签名检查（直到运行时） |
| `ABC`（抽象基类） | 编译时类型检查；IDE 自动补全 | 必须显式继承；耦合到 DeerFlow 的基类 |

**选择 Protocol**：`GuardrailProvider` 使用 `@runtime_checkable` 装饰的 `Protocol`，定义三个成员：`name: str`、`evaluate()` 和 `aevaluate()`。Provider 实现者不需要导入 DeerFlow 的任何类型——纯 Python 类即可满足协议。`resolve_variable()` 按类路径加载时，不需要校验继承关系，只需检查对象是否有正确的方法签名。

**为什么同时需要同步和异步方法**：`AgentMiddleware` 框架有 `wrap_tool_call`（同步）和 `awrap_tool_call`（异步）两条路径。Provider 需要同时支持两种调用方式。简单 Provider（如 `AllowlistProvider`）在 `aevaluate` 中直接委托给 `evaluate`，避免重复代码。

---

### 决策 2：fail_closed 默认策略

**问题**：Provider 自身抛出异常时，应该放行还是拒绝？

| 方案 | 优势 | 劣势 |
|------|------|------|
| fail_closed=True（当前默认） | 安全优先；Provider 崩溃不等于无限制访问 | 可能因 Provider bug 误杀合法调用 |
| fail_closed=False | 可用性优先；Provider 崩溃不影响正常操作 | Provider 故障 = 安全系统失效 |

**选择 fail_closed**：`GuardrailMiddleware.__init__()` 默认 `fail_closed=True`。当 `provider.evaluate()` 抛出非 `GraphBubbleUp` 异常时，自动构造 `GuardrailDecision(allow=False, reasons=[GuardrailReason(code="oap.evaluator_error")])`。这确保安全系统的默认策略是"不确定就拒绝"。

**GraphBubbleUp 例外**：LangGraph 的 interrupt/pause/resume 控制流信号通过 `GraphBubbleUp` 异常传递。如果被 fail_closed 策略捕获并返回拒绝 ToolMessage，工作流暂停机制会失效。因此必须在 `except` 之前单独 `raise` 透传。

**用户可选**：配置中 `fail_closed: false` 允许用户在特定场景下（如开发调试）切换为放行策略。

---

### 决策 3：evaluate 在 wrap_tool_call 中执行

**问题**：安全评估应该发生在哪个生命周期钩子中？

| 钩子 | 时机 | 可行性 |
|------|------|--------|
| `before_agent` | ReAct 循环开始前 | 太早——还不知道会调用哪些工具 |
| `wrap_model_call` | LLM 调用前 | 太早——只有工具 schema，没有实际参数 |
| `wrap_tool_call`（当前） | 工具执行前 | 正确——有工具名 + 实际参数 |
| `after_agent` | Agent 执行完成后 | 太晚——工具已经执行 |

**选择 wrap_tool_call**：`GuardrailMiddleware` 重写 `wrap_tool_call` 和 `awrap_tool_call`，在 `handler(request)` 之前执行 `provider.evaluate()`。此时 `ToolCallRequest` 包含完整的工具名（`tool_call["name"]`）和参数（`tool_call["args"]`），Provider 有足够信息做决策。

**为什么不在 after_model 中做**：`after_model` 能看到 AIMessage 中的 `tool_calls`，但此时工具尚未执行，且 `after_model` 钩子无法阻止工具执行——只能修改 AIMessage。`wrap_tool_call` 是唯一个既能看到实际参数又能阻止执行的钩子。

---

### 决策 4：AllowlistProvider 作为内置 Provider

**问题**：最常见的安全需求是什么？需要安装额外依赖吗？

| 需求 | 复杂度 | 覆盖率 |
|------|--------|--------|
| 只允许特定工具 | 低 | 高（80% 的使用场景） |
| 基于参数内容的策略 | 中 | 中 |
| OAP 护照 + 命令审计 | 高 | 低（企业场景） |

**选择内置 AllowlistProvider**：零外部依赖，仅做 `set` 查找（O(1)），覆盖最常见的"只允许/禁止特定工具"需求。配置示例：

```yaml
guardrails:
  enabled: true
  provider:
    use: "deerflow.guardrails.builtin:AllowlistProvider"
    config:
      allowed_tools: ["read_file", "web_search", "present_files"]
```

**评估顺序**：先检查白名单（若配置），再检查黑名单。不在白名单 → 拒绝；在白名单但在黑名单 → 也拒绝。`_allowed` 为 None 表示不启用白名单（所有工具默认允许），空 set 表示空白名单（所有工具都被拒绝）。

---

### 决策 5：OAP 标准对齐

**问题**：Guardrails 的数据结构是否应该遵循某个标准？

| 方案 | 优势 | 劣势 |
|------|------|------|
| OAP 对齐（当前） | 标准化原因码；便于集成企业监控系统 | OAP 规范本身仍在演进 |
| 自定义格式 | 自由度高 | 每个 Provider 自定义错误码，不可互操作 |

**选择 OAP 对齐**：`GuardrailReason` 使用 OAP 标准的 `code` + `message` 结构。常见原因码包括：`oap.allowed`、`oap.denied`、`oap.tool_not_allowed`、`oap.command_not_allowed`、`oap.evaluator_error`。`GuardrailDecision` 的 `policy_id` 字段支持审计追踪。

**为什么 Request 包含 agent_id**：OAP Provider 需要护照路径来定位 Agent 的权限声明。内置 `AllowlistProvider` 不需要 `agent_id`，但数据结构保持一致，高级 Provider 可以使用。

---

## 三、实现效果

| 效果 | 实现方式 |
|------|----------|
| **可插拔策略** | Protocol 接口 + resolve_variable 反射加载 |
| **零依赖基础安全** | 内置 AllowlistProvider，set 查找 O(1) |
| **安全优先** | fail_closed=True 默认，Provider 异常时拒绝 |
| **Agent 自愈** | 拒绝时返回 ToolMessage(status="error")，Agent 可选择替代方案 |
| **控制流保护** | GraphBubbleUp 直接透传，不破坏 LangGraph 机制 |
| **审计友好** | OAP 标准原因码 + policy_id 追溯 |
| **同步/异步双路径** | evaluate + aevaluate 分别覆盖 sync/async 调用 |
