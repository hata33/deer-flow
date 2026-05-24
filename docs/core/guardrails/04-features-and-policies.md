# Guardrails 其他特性与策略

> 深入探讨 Guardrails 系统的设计决策、安全策略、扩展机制、测试策略、以及与其他系统的协作关系。

---

## 一、安全策略详解

### 1.1 Fail-Closed vs Fail-Open

Guardrails 的核心安全哲学是"默认拒绝"（deny-by-default），体现在两个层面：

**Provider 异常时的 fail_closed**：

```
provider.evaluate() 抛出异常
    │
    ├─ fail_closed=True (默认，推荐生产环境)
    │   └─ 构建 GuardrailDecision(allow=False, code="oap.evaluator_error")
    │   └─ Agent 收到: "guardrail provider error (fail-closed)"
    │   └─ 风险: 零（最坏情况是误杀合法调用）
    │
    └─ fail_closed=False (开发/测试环境)
        └─ logger.exception() + return handler(request)
        └─ Agent 工具正常执行
        └─ 风险: Provider 崩溃时 Agent 获得不受限制的工具访问权
```

**为什么默认 fail_closed=True**：
- 安全系统的基础原则是"不确定就拒绝"
- 误杀合法调用比放行恶意调用代价小得多
- Agent 可自愈：看到错误后可选择替代方案

**配置默认关闭**：
```yaml
guardrails:
  enabled: false  # 默认不启用 —— 用户显式选择开启安全防护
```

### 1.2 GraphBubbleUp 透传

LangGraph 的控制流信号（`GraphBubbleUp`）用于实现 `interrupt()` 暂停和 `Command(resume=...)` 恢复机制。GuardrailMiddleware 必须将这些异常原样透传，否则会破坏 LangGraph 的工作流控制。

```python
try:
    decision = self.provider.evaluate(gr)
except GraphBubbleUp:
    raise  # 直接抛出，不捕获
except Exception:
    # 处理真正的 Provider 错误
    ...
```

**测试覆盖**：`test_guardrail_middleware.py` 中有专门的 `test_graph_bubble_up_not_swallowed` 和 `test_async_graph_bubble_up_not_swallowed` 测试。

### 1.3 空 Reasons 回退

Provider 实现可能存在 bug，返回 `allow=False` 但 `reasons=[]`。Middleware 通过回退机制确保 Agent 仍能收到有意义的错误信息：

```python
reason_text = decision.reasons[0].message if decision.reasons else "blocked by guardrail policy"
reason_code = decision.reasons[0].code if decision.reasons else "oap.denied"
```

---

## 二、Provider 扩展机制

### 2.1 Protocol vs ABC 的设计选择

```
┌──────────────────────────────────────────────────────────────┐
│                   Provider 接口设计                            │
├─────────────────────┬────────────────────────────────────────┤
│ Protocol (当前)      │ ABC (未采用)                            │
├─────────────────────┼────────────────────────────────────────┤
│ 结构子类型           │ 名义子类型                               │
│ 无需显式继承          │ 必须显式继承                             │
│ @runtime_checkable   │ isinstance 自然支持                      │
│ 纯 Python 类即可      │ 必须 import DeerFlow 类型               │
│ 降低耦合              │ 增加耦合                                │
└─────────────────────┴────────────────────────────────────────┘
```

**为什么选 Protocol**：
1. Provider 实现者不需要导入 DeerFlow 的任何类型，降低依赖
2. 纯 Python 类即可满足协议，灵活性最高
3. `resolve_variable()` 按类路径加载，不关心继承关系
4. `@runtime_checkable` 允许测试中验证协议兼容性

**Protocol 的局限**：
- IDE 无法为 Protocol 方法提供自动补全提示
- 类型检查器（mypy/pyright）的静态检查不如 ABC 精确
- 但与反射加载的灵活性相比，这些代价可以接受

### 2.2 resolve_variable() 反射加载

DeerFlow 使用统一的反射加载机制处理所有可插拔组件（模型、工具、沙箱、Guardrails Provider）：

```python
# 类路径格式: package.module:ClassName
resolve_variable("deerflow.guardrails.builtin:AllowlistProvider")
# → <class AllowlistProvider>

resolve_variable("my_package.providers:MyProvider")
# → <class MyProvider>
```

**framework 参数注入**：
```python
# 检查 Provider 构造函数是否接受 framework 参数
sig = inspect.signature(provider_cls.__init__)
if "framework" in sig.parameters or any(p.kind == VAR_KEYWORD for p in sig.parameters.values()):
    provider_kwargs["framework"] = "deerflow"
```

**为什么需要 framework 参数**：
- OAP Provider（如 APort）需要知道自己在 DeerFlow 中运行，以定位配置文件目录
- 内置 AllowlistProvider 不需要，通过 `inspect.signature` 检测后跳过
- 使用 `**kwargs` 的 Provider 自动兼容

### 2.3 三种 Provider 的适用场景

| Provider | 适用场景 | 优势 | 劣势 |
|----------|---------|------|------|
| AllowlistProvider | 快速上手、简单限制 | 零依赖、配置简单 | 仅能按工具名匹配 |
| OAP Provider | 企业级安全、多 Agent | 细粒度策略、标准化 | 需要外部依赖 |
| 自定义 Provider | 特殊业务需求 | 完全灵活 | 需要自行开发和维护 |

---

## 三、配置管理策略

### 3.1 单例模式

```
_get_guardrails_config (模块级变量)
    │
    ├─ 首次调用 → GuardrailsConfig() 返回默认值
    │
    ├─ AppConfig.from_file() → load_guardrails_config_from_dict()
    │   └─ 覆盖单例为实际配置
    │
    └─ 后续调用 → 返回已加载的配置
```

**为什么用单例**：
- Guardrails 配置在应用启动时确定，运行期间不变
- 避免到处传递 AppConfig 对象
- 支持测试中的 `reset_guardrails_config()` 隔离

### 3.2 配置优先级

```
config.yaml 中的 guardrails 块
    ↓ (最高优先级)
环境变量: 暂未支持（GuardrailsConfig 字段无 env 绑定）
    ↓
默认值: enabled=False, fail_closed=True
    ↓ (最低优先级)
```

### 3.3 enabled 默认值

```python
class GuardrailsConfig(BaseModel):
    enabled: bool = Field(default=False, ...)  # 默认不启用
```

**为什么默认不启用**：
- Guardrails 是可选的增强安全层，不是核心功能
- 大多数用户在开发/测试阶段不需要策略限制
- 需要用户显式配置 `enabled: true` 来选择加入

---

## 四、双路径（同步/异步）支持

### 4.1 为什么需要两套方法

AgentMiddleware 框架定义了 `wrap_tool_call`（同步）和 `awrap_tool_call`（异步）两个钩子。LangGraph 的 `ToolNode` 在不同上下文中会调用不同的路径：

- 同步图执行（`invoke`）→ `wrap_tool_call`
- 异步图执行（`ainvoke`）→ `awrap_tool_call`
- 流式执行（`stream`）→ 取决于 stream mode

Provider 也需要同时实现 `evaluate()` 和 `aevaluate()`，因为 Provider 可能依赖异步 I/O（如 OAP Provider 做网络请求）。

### 4.2 AllowlistProvider 的简化处理

```python
class AllowlistProvider:
    def evaluate(self, request):
        # 纯内存操作
        ...

    async def aevaluate(self, request):
        return self.evaluate(request)  # 直接委托
```

AllowlistProvider 是纯内存操作（set lookup），无需异步实现。`aevaluate` 直接委托给 `evaluate` 避免代码重复。

---

## 五、错误消息设计策略

### 5.1 OAP 原因码体系

Guardrails 使用 OAP（Open Agent Passport）标准的 reason codes，确保错误消息的一致性和可解释性：

| 原因码 | 含义 | 触发场景 |
|--------|------|---------|
| `oap.allowed` | 授权通过 | Provider 判定允许 |
| `oap.denied` | 通用拒绝 | 回退使用 |
| `oap.tool_not_allowed` | 工具不在允许列表中 | AllowlistProvider 白名单/黑名单拒绝 |
| `oap.command_not_allowed` | 命令不被允许 | OAP Provider 命令限制 |
| `oap.blocked_pattern` | 匹配到禁止模式 | OAP Provider 模式拦截 |
| `oap.limit_exceeded` | 超出限额 | OAP Provider 限制检查 |
| `oap.passport_suspended` | 护照已暂停/吊销 | OAP Provider 状态检查 |
| `oap.evaluator_error` | Provider 评估错误 | Provider 异常时的回退码 |

### 5.2 Agent 自愈机制

拒绝消息的设计原则是"不仅告诉 Agent 什么被拒绝了，还要引导它怎么做"：

```
ToolMessage content:
"Guardrail denied: tool 'bash' was blocked (oap.tool_not_allowed).
 Reason: tool 'bash' not in allowlist.
 Choose an alternative approach."
                        ^^^^^^^^^^^^^^^^^^^^^^^^
                        引导 Agent 尝试替代方案
```

- `tool_call_id` 被正确设置，确保 LangGraph 能匹配请求和响应
- `name` 被设置为被拒绝的工具名，便于 Agent 理解上下文
- `status="error"` 触发 Agent 的错误处理逻辑

---

## 六、测试策略

### 6.1 测试覆盖

`test_guardrail_middleware.py` 包含 25+ 个测试用例，覆盖：

| 测试类别 | 测试内容 |
|---------|---------|
| AllowlistProvider 基础 | 无限制、仅拒绝、仅允许、组合使用、异步委托 |
| Middleware 放行 | 允许的调用正常通过，handler 被调用 |
| Middleware 拒绝 | 拒绝返回 error ToolMessage，handler 不被调用 |
| Fail-closed | 同步/异步 Provider 异常时阻止调用 |
| Fail-open | 同步/异步 Provider 异常时放行调用 |
| Passport 传递 | agent_id 正确传递给 Provider |
| OAP 原因码 | 拒绝消息中包含正确的 OAP code |
| 空 reasons 回退 | deny 但 reasons=[] 时使用回退文本 |
| 空工具名 | 工具名为空字符串时优雅处理 |
| Protocol 检查 | isinstance(AllowlistProvider(), GuardrailProvider) |
| GraphBubbleUp | 同步/异步 GraphBubbleUp 未被捕获 |
| 配置管理 | 默认值、from_dict、单例加载/重置 |

### 6.2 测试 Provider 模式

```python
# 测试用 Provider 直接实现协议，无需继承
class _DenyAllProvider:
    name = "deny-all"

    def evaluate(self, request):
        return GuardrailDecision(
            allow=False,
            reasons=[GuardrailReason(code="oap.denied", message="all tools blocked")],
            policy_id="test.deny.v1",
        )

    async def aevaluate(self, request):
        return self.evaluate(request)

# 立即用于测试
mw = GuardrailMiddleware(_DenyAllProvider())
```

**优势**：测试 Provider 不需要任何 mock 框架，纯 Python 类即可。

---

## 七、与其他系统的协作

### 7.1 与 Skills 的 tool_policy 对比

| 维度 | Guardrails (本系统) | Skills tool_policy |
|------|-------------------|-------------------|
| 粒度 | 每次工具调用 | Agent 启动时一次性过滤 |
| 决策来源 | Provider 策略评估 | SKILL.md 中的 allowed-tools 声明 |
| 可插拔 | 是（三种 Provider） | 否（内置逻辑） |
| 配置方式 | config.yaml | extensions_config.json |
| 拒绝方式 | ToolMessage error | 工具根本不注入 Agent |
| 运行时开销 | 每次调用都有 | 仅在 Agent 构建时 |

两者互补：Skills tool_policy 在 Agent 构建时移除工具，Guardrails 在运行时拦截每次调用。

### 7.2 与 Sandbox 的协作

```
Guardrails 和 Sandbox 从不同维度保障安全：

Sandbox (进程级隔离):
  ✓ 防止文件系统越界访问
  ✓ 限制网络访问
  ✓ 资源限制（CPU/内存）

Guardrails (策略级授权):
  ✓ 禁止特定工具（如禁止 bash）
  ✓ 限制命令参数（如禁止 rm -rf）
  ✓ 基于上下文的动态决策（如子 Agent 更严格）
```

**典型攻击场景防御**：
- Sandbox 无法阻止 `curl` 数据外传（因为网络可能已开放）
- Guardrails 可以：配置 `denied_tools: ["bash"]`，Agent 无法执行任何命令

### 7.3 与 Human-in-the-Loop 的协作

```
高风险操作的三层防护：
  1. Guardrails → 自动拒绝明确违规的操作
  2. ask_clarification → 人工审批模糊/高风险操作
  3. 沙箱隔离 → 即使执行也在受限环境中

层级关系：
  Guardrails 自动 → 人工审批 → 沙箱执行
  (最快)           (需等待)     (最底层)
```

---

## 八、设计权衡总结

| 设计决策 | 选择 | 权衡 |
|---------|------|------|
| Protocol vs ABC | Protocol | 灵活性 > IDE 支持 |
| fail_closed 默认 | True | 安全性 > 可用性 |
| enabled 默认 | False | 非侵入性 > 默认安全 |
| 单例配置 | 是 | 简单性 > 动态更新灵活性 |
| OAP 原因码 | 对齐 | 标准化 > 自定义灵活性 |
| 空 reasons 回退 | 硬编码 | 鲁棒性 > 精确性 |
| framework 注入 | inspect 检测 | 兼容性 > 简单性 |
| GraphBubbleUp 透传 | 特殊处理 | 正确性 > 统一异常处理 |
| 同步+异步双路径 | 独立实现 | 完整性 > DRY |

---

## 九、文件索引

```
packages/harness/deerflow/guardrails/
    __init__.py              # 模块入口 + 公开导出
    provider.py              # GuardrailProvider Protocol, GuardrailRequest, GuardrailDecision, GuardrailReason
    middleware.py             # GuardrailMiddleware (AgentMiddleware 子类)
    builtin.py               # AllowlistProvider (零依赖内置实现)

packages/harness/deerflow/config/
    guardrails_config.py     # GuardrailsConfig Pydantic 模型 + 单例管理

packages/harness/deerflow/agents/middlewares/
    tool_error_handling_middleware.py  # GuardrailMiddleware 注册点

packages/harness/deerflow/agents/
    factory.py               # RuntimeFeatures.guardrail 控制开关
    features.py              # RuntimeFeatures 数据类定义

config.example.yaml          # 三种 Provider 配置示例

tests/
    test_guardrail_middleware.py   # 25+ 测试用例
    test_create_deerflow_agent.py  # Agent 构建时的 guardrail 集成测试

docs/
    backend/docs/GUARDRAILS.md     # 用户面向的完整使用文档
    docs/core/guardrails/          # 开发者面向的架构文档 (本系列)
```
