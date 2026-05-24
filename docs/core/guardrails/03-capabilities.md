# Guardrails 运行时能力清单与来源

> Guardrails 系统在运行时提供了哪些能力？每种能力由哪个模块实现？依赖什么配置？

---

## 一、能力全景图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Guardrails 运行时能力                                 │
├───────────────┬───────────────┬───────────────┬───────────────┬─────────┤
│ 工具调用拦截   │ 策略评估       │ 安全回退       │ 错误反馈       │ 配置管理  │
│ (Middleware)  │ (Provider)    │ (Fail-closed) │ (ToolMessage) │ (Config) │
├───────────────┼───────────────┼───────────────┼───────────────┼─────────┤
│ wrap_tool_    │ evaluate()    │ GraphBubbleUp │ ToolMessage   │ Guard-   │
│ call()        │ aevaluate()   │ 透传          │ status=error  │ rails    │
│ awrap_tool_   │ Guardrail-    │ 异常→拒绝决策  │ OAP 原因码     │ Config   │
│ call()        │ Decision      │ fail_open     │ Agent 自愈     │ 单例     │
└───────────────┴───────────────┴───────────────┴───────────────┴─────────┘
```

---

## 二、能力详细清单

### 能力 1：工具调用同步拦截

**说明**：在每次同步工具调用执行前，构建 GuardrailRequest，调用 Provider 的同步评估方法，根据决策决定放行或拒绝。

**实现模块**：`guardrails/middleware.py` → `GuardrailMiddleware.wrap_tool_call()`

**调用链**：
```
Agent 调用工具
  → 中间件链
    → GuardrailMiddleware.wrap_tool_call(request, handler)
      → _build_request(request) → GuardrailRequest
      → provider.evaluate(gr) → GuardrailDecision
      → allow? handler(request) : _build_denied_message(request, decision)
```

**配置依赖**：`guardrails.enabled=True` + `guardrails.provider` 配置

**运行时依赖**：
- `guardrails_config.fail_closed` — 异常处理策略
- `guardrails_config.passport` — 注入到 request.agent_id

**代码位置**：
- `middleware.py` 第 ~120 行 `wrap_tool_call()`
- `middleware.py` 第 ~95 行 `_build_request()`
- `middleware.py` 第 ~108 行 `_build_denied_message()`

---

### 能力 2：工具调用异步拦截

**说明**：与同步拦截逻辑一致，但调用 Provider 的异步评估方法 `aevaluate()`，适用于异步工具调用场景。

**实现模块**：`guardrails/middleware.py` → `GuardrailMiddleware.awrap_tool_call()`

**调用链**：
```
Agent 调用工具 (async context)
  → 中间件链
    → GuardrailMiddleware.awrap_tool_call(request, handler)
      → _build_request(request) → GuardrailRequest
      → await provider.aevaluate(gr) → GuardrailDecision
      → allow? await handler(request) : _build_denied_message(request, decision)
```

**配置依赖**：同同步拦截

**代码位置**：`middleware.py` 第 ~143 行 `awrap_tool_call()`

---

### 能力 3：白名单/黑名单策略评估

**说明**：基于工具名进行简单的允许/拒绝判断。支持白名单（仅允许列表中的工具）、黑名单（拒绝列表中的工具）、以及两者组合。

**实现模块**：`guardrails/builtin.py` → `AllowlistProvider`

**评估规则**：
1. 若配置了 `allowed_tools` 且工具不在其中 → 拒绝
2. 若工具在 `denied_tools` 中 → 拒绝
3. 否则 → 允许

```python
# 示例
AllowlistProvider(allowed_tools=["web_search", "read_file"])  # 白名单模式
AllowlistProvider(denied_tools=["bash", "write_file"])         # 黑名单模式
AllowlistProvider(allowed_tools=["web_search"], denied_tools=["bash"])  # 组合
```

**配置依赖**：
```yaml
guardrails:
  enabled: true
  provider:
    use: deerflow.guardrails.builtin:AllowlistProvider
    config:
      allowed_tools: ["web_search", "read_file"]  # 或 denied_tools: [...]
```

**运行时依赖**：无外部依赖，纯内存 set lookup

**代码位置**：`builtin.py` 第 ~49 行 `evaluate()`

---

### 能力 4：OAP 护照策略评估

**说明**：基于 Open Agent Passport (OAP) 开放标准的策略评估。Provider 读取护照 JSON 文件，检查 Agent 的能力声明、命令限制、护照状态等。

**实现模块**：第三方包（如 `aport_guardrails`），通过 Protocol 集成

**评估维度**：

| 维度 | 护照字段 | 拒绝原因码 |
|------|---------|-----------|
| 工具类别授权 | `capabilities[].id` | `oap.tool_not_allowed` |
| 命令白名单 | `limits.*.allowed_commands` | `oap.command_not_allowed` |
| 禁止模式 | `limits.*.blocked_patterns` | `oap.blocked_pattern` |
| 护照状态 | `status` | `oap.passport_suspended` |
| 限额超限 | `limits` | `oap.limit_exceeded` |

**配置依赖**：
```yaml
guardrails:
  enabled: true
  provider:
    use: aport_guardrails.providers.generic:OAPGuardrailProvider
```

**运行时依赖**：
- 护照 JSON 文件（由 `aport setup` 生成或手动创建）
- `guardrails_config.passport` — 护照文件路径

**代码位置**：第三方包，通过 `resolve_variable()` 加载

---

### 能力 5：自定义策略评估

**说明**：任何 Python 类实现 `evaluate(GuardrailRequest) -> GuardrailDecision` 和 `aevaluate` 方法，即可作为自定义 Provider。可检查工具名、参数内容、调用上下文等任意信息。

**实现模块**：用户自定义模块

**示例**：
```python
class MyGuardrailProvider:
    name = "my-company"

    def evaluate(self, request):
        # 阻止任何包含 "delete" 的 bash 命令
        if request.tool_name == "bash" and "delete" in str(request.tool_input):
            return GuardrailDecision(
                allow=False,
                reasons=[GuardrailReason(code="custom.blocked", message="delete not allowed")],
                policy_id="custom.v1",
            )
        return GuardrailDecision(allow=True, reasons=[GuardrailReason(code="oap.allowed")])

    async def aevaluate(self, request):
        return self.evaluate(request)
```

**配置依赖**：
```yaml
guardrails:
  enabled: true
  provider:
    use: my_guardrail:MyGuardrailProvider
    config:
      custom_param: value
```

**代码位置**：用户项目目录

---

### 能力 6：安全回退（Fail-Closed / Fail-Open）

**说明**：当 Provider 抛出异常时，根据 `fail_closed` 配置决定行为。

**实现模块**：`guardrails/middleware.py` → `wrap_tool_call()` / `awrap_tool_call()` 中的异常处理

**两种模式**：

| 模式 | 行为 | 适用场景 |
|------|------|---------|
| fail_closed=True（默认） | 阻止调用，返回 `oap.evaluator_error` | 生产环境，安全优先 |
| fail_closed=False | 放行调用，记录警告 | 开发/测试环境，可用性优先 |

**异常分类处理**：
```
provider.evaluate() 抛出异常
  ├─ GraphBubbleUp   → 直接抛出（LangGraph 控制流信号，不是错误）
  └─ 其他 Exception  → 按 fail_closed 策略处理
```

**为什么 GraphBubbleUp 必须透传**：
LangGraph 的 `interrupt()` / `Command(resume=...)` 机制依赖 GraphBubbleUp 异常传播。如果被 GuardrailMiddleware 的 try/except 捕获，会导致工作流暂停/恢复机制失效。

**代码位置**：`middleware.py` 第 ~130-135 行（同步），第 ~162-167 行（异步）

---

### 能力 7：结构化错误反馈（OAP ToolMessage）

**说明**：被拒绝的工具调用返回结构化错误消息，Agent 可理解拒绝原因并选择替代方案。

**实现模块**：`guardrails/middleware.py` → `_build_denied_message()`

**ToolMessage 结构**：
```python
ToolMessage(
    content="Guardrail denied: tool 'bash' was blocked (oap.tool_not_allowed). "
            "Reason: tool 'bash' not in allowlist. "
            "Choose an alternative approach.",
    tool_call_id="call_1",
    name="bash",
    status="error",
)
```

**关键设计**：
- `status="error"` — 告诉 Agent 工具调用失败
- OAP 原因码 — 标准化的错误分类
- "Choose an alternative approach" — 引导 Agent 自愈

**代码位置**：`middleware.py` 第 ~108-124 行

---

### 能力 8：配置单例管理

**说明**：Guardrails 配置以模块级单例方式管理，应用启动时加载一次，运行期间通过 `get_guardrails_config()` 获取。

**实现模块**：`config/guardrails_config.py`

**API**：

| 函数 | 用途 |
|------|------|
| `get_guardrails_config()` | 获取当前配置（首次调用返回默认） |
| `load_guardrails_config_from_dict(data)` | 从 dict 加载配置 |
| `reset_guardrails_config()` | 重置单例（测试用） |

**默认值**：
- `enabled=False` — 默认不启用
- `fail_closed=True` — 默认安全优先
- `passport=None` — 默认无护照
- `provider=None` — 默认无 Provider

**代码位置**：`config/guardrails_config.py`

---

### 能力 9：Provider Protocol 运行时检查

**说明**：通过 `@runtime_checkable` 装饰的 Protocol，可在运行时用 `isinstance()` 检查一个对象是否满足 GuardrailProvider 协议。

**实现模块**：`guardrails/provider.py` → `GuardrailProvider(Protocol)`

```python
from deerflow.guardrails.provider import GuardrailProvider
from deerflow.guardrails.builtin import AllowlistProvider

# 运行时检查
assert isinstance(AllowlistProvider(), GuardrailProvider)  # True
```

**代码位置**：`provider.py` 第 ~98 行

---

### 能力 10：Provider 反射加载

**说明**：通过 `resolve_variable()` 按类路径字符串动态加载 Provider 类，与模型、工具、沙箱使用同一套反射机制。

**实现模块**：`deerflow.reflection.resolve_variable`

**类路径格式**：`package.module:ClassName`

**示例**：
```python
resolve_variable("deerflow.guardrails.builtin:AllowlistProvider")  # → AllowlistProvider 类
resolve_variable("my_package:MyProvider")                          # → MyProvider 类
```

**代码位置**：`agents/middlewares/tool_error_handling_middleware.py` 第 ~107 行

---

## 三、能力-模块映射表

| 能力 | 模块 | 关键函数/类 | 配置依赖 |
|------|------|-----------|---------|
| 同步拦截 | `middleware.py` | `GuardrailMiddleware.wrap_tool_call()` | guardrails.enabled |
| 异步拦截 | `middleware.py` | `GuardrailMiddleware.awrap_tool_call()` | guardrails.enabled |
| 白名单/黑名单 | `builtin.py` | `AllowlistProvider.evaluate()` | provider.use + config |
| OAP 护照 | 第三方 | `OAPGuardrailProvider.evaluate()` | provider.use |
| 自定义策略 | 用户代码 | 自定义 `evaluate()` | provider.use + config |
| Fail-closed | `middleware.py` | 异常处理逻辑 | guardrails.fail_closed |
| Fail-open | `middleware.py` | 异常处理逻辑 | guardrails.fail_closed=false |
| GraphBubbleUp 透传 | `middleware.py` | `except GraphBubbleUp: raise` | 无（始终生效） |
| 错误反馈 | `middleware.py` | `_build_denied_message()` | 无（始终生效） |
| 配置管理 | `guardrails_config.py` | `get/load/reset_guardrails_config()` | guardrails 配置块 |
| 协议检查 | `provider.py` | `GuardrailProvider(Protocol)` | 无（始终可用） |
| 反射加载 | `reflection.py` | `resolve_variable()` | provider.use |

---

## 四、能力生效条件

```
Guardrails 生效条件:
  ✓ config.yaml 中 guardrails.enabled = true
  ✓ guardrails.provider 配置了有效的 use 路径
  ✓ use 指向的类可被 resolve_variable() 成功加载
  ✓ 类实现了 evaluate() 和 aevaluate() 方法
  ✓ 类有 name 属性

若不满足以上任何条件:
  → GuardrailMiddleware 不会被注册到中间件链
  → 所有工具调用直接放行（无 Guardrails 保护）
```
