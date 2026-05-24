# Guardrails 完整生命周期

> 从配置加载到每次工具调用的完整过程：配置 → 实例化 → 中间件注册 → 请求构建 → 策略评估 → 决策 → 拒绝/放行。每一步涉及哪些模块、做了什么决策、有哪些安全边界。

---

## 生命周期全景

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                        Guardrails 生命周期                                    │
├──────────┬──────────┬──────────┬──────────┬──────────┬──────────┬───────────┤
│ ① 配置   │ ② 加载   │ ③ 实例化 │ ④ 注册   │ ⑤ 拦截   │ ⑥ 评估   │ ⑦ 决策    │
│ YAML 定义│ 单例注入 │ 反射创建  │ 中间件链  │ 请求构建  │ Provider  │ 拒绝/放行  │
├──────────┼──────────┼──────────┼──────────┼──────────┼──────────┼───────────┤
│config.   │app_      │resolve_  │tool_     │_build_   │provider. │_build_    │
│yaml      │config.py │variable  │error_    │request() │evaluate  │denied_    │
│          │          │()        │handling  │          │()        │message()  │
│          │          │          │.py       │          │          │           │
└──────────┴──────────┴──────────┴──────────┴──────────┴──────────┴───────────┘
```

---

## 阶段 ①：配置（在哪定义、怎么配置）

### 配置来源

```
配置来源
├── config.yaml                    # 主配置文件，guardrails 配置块
├── config.example.yaml            # 示例配置（三种 Provider 选项）
└── backend/docs/GUARDRAILS.md     # 完整配置文档
```

### 配置结构

```yaml
# config.yaml 中的 guardrails 配置块
guardrails:
  enabled: true                    # 启用/禁用（默认 false）
  fail_closed: true                # Provider 异常时是否阻止（默认 true）
  passport: null                   # 护照路径或托管 Agent ID（可选）
  provider:
    use: deerflow.guardrails.builtin:AllowlistProvider  # Provider 类路径
    config:                        # Provider 构造参数
      denied_tools: ["bash", "write_file"]
```

### 三种 Provider 配置选项

| 选项 | use 字段 | config 示例 |
|------|---------|------------|
| 内置 Allowlist | `deerflow.guardrails.builtin:AllowlistProvider` | `denied_tools: ["bash"]` 或 `allowed_tools: ["web_search"]` |
| OAP 护照 | `aport_guardrails.providers.generic:OAPGuardrailProvider` | 由 `aport setup` 自动生成 |
| 自定义 | `my_package:MyProvider` | 任意键值对，传给 Provider.__init__ |

**涉及文件**：
- `config/guardrails_config.py` — `GuardrailsConfig` 和 `GuardrailProviderConfig` Pydantic 模型
- `config/app_config.py` — `AppConfig.guardrails` 字段，`_apply_singleton_configs()` 中加载

---

## 阶段 ②：加载（配置如何变成运行时可用的单例）

### 加载流程

```
config.yaml
    │
    ▼ AppConfig.from_file()
   yaml.safe_load() → config_data dict
    │
    ▼ AppConfig.model_validate(config_data)
   AppConfig.guardrails: GuardrailsConfig
    │
    ▼ AppConfig._apply_singleton_configs()
   load_guardrails_config_from_dict(config.guardrails.model_dump())
    │
    ▼ 存入模块级单例 _guardrails_config
    │
    ▼ 后续调用 get_guardrails_config() 获取
```

### 为什么使用单例模式

- Guardrails 配置在应用启动时加载一次，运行期间不变（除非通过 API 显式重新加载）
- 单例避免了到处传递 AppConfig 对象
- 测试中可通过 `reset_guardrails_config()` 清理状态

**涉及函数**：
- `load_guardrails_config_from_dict(data)` — 从 dict 加载配置到单例
- `get_guardrails_config()` — 获取当前配置（首次调用时返回默认配置）
- `reset_guardrails_config()` — 重置单例（测试用）

---

## 阶段 ③：实例化（如何从配置创建 Provider 实例）

### 实例化流程

```
get_guardrails_config()
    │
    ▼ guardrails_config.enabled == True?
    │
    ├─ False → 跳过（不注册 GuardrailMiddleware）
    │
    └─ True + provider 配置存在
         │
         ▼ resolve_variable(guardrails_config.provider.use)
         反射加载 Provider 类
         │
         ▼ 构建 kwargs
         provider.config 中的键值对 + framework="deerflow"
         │
         ▼ 检查 Provider.__init__ 签名
         若接受 framework 参数 → 注入 framework="deerflow"
         若接受 **kwargs → 同样注入
         │
         ▼ provider_cls(**provider_kwargs)
         Provider 实例
```

### 为什么注入 framework="deerflow"

- OAP Provider（如 APort）需要知道自己在哪个框架中运行，以确定配置文件目录
- 内置 AllowlistProvider 的 `__init__` 不接受 framework 参数，通过 `inspect.signature` 检测后跳过
- 使用 `**kwargs` 的 Provider 自动兼容

**涉及函数**：
- `resolve_variable(class_path)` — DeerFlow 通用反射加载器（与模型、工具、沙箱共用）
- `inspect.signature(provider_cls.__init__)` — 检查构造函数签名

---

## 阶段 ④：注册（中间件如何插入中间件链）

### 注册位置

GuardrailMiddleware 被插入到中间件链的第 4 位（从 0 开始计数）：

```
中间件链顺序（make_lead_agent）:
[0] ThreadDataMiddleware        — 线程数据初始化
[1] UploadsMiddleware           — 文件上传追踪
[2] SandboxMiddleware           — 沙箱获取
[3] DanglingToolCallMiddleware  — 修复不完整的工具调用
[4] GuardrailMiddleware         ◄── 本中间件
[5] ToolErrorHandlingMiddleware — 工具异常转错误消息
[6] SummarizationMiddleware     — 对话摘要
[7] TodoMiddleware              — 计划模式
[8] TitleMiddleware             — 自动标题
[9] MemoryMiddleware            — 记忆系统
[10] ViewImageMiddleware        — 图片查看
[11] SubagentLimitMiddleware    — 子 Agent 限制
[12] LoopDetectionMiddleware    — 循环检测
[13] ClarificationMiddleware    — 澄清问题
```

### 为什么放在第 4 位

1. **在沙箱之后**：此时工具调用上下文已完整构建（thread_data、uploads、sandbox 已就绪）
2. **在 ToolErrorHandling 之前**：拒绝消息也需要被 ToolErrorHandling 兜底（但通常不会，因为 GuardrailMiddleware 自己构建了完整的 ToolMessage）
3. **在业务中间件之前**：Summarization、Memory 等业务中间件不应该看到被 Guardrails 拒绝的调用

### 注册代码路径

```python
# agents/middlewares/tool_error_handling_middleware.py → _build_runtime_middlewares()

guardrails_config = app_config.guardrails
if guardrails_config.enabled and guardrails_config.provider:
    provider_cls = resolve_variable(guardrails_config.provider.use)
    provider = provider_cls(**provider_kwargs)
    middlewares.append(GuardrailMiddleware(
        provider,
        fail_closed=guardrails_config.fail_closed,
        passport=guardrails_config.passport,
    ))
```

---

## 阶段 ⑤：拦截（每次工具调用时发生了什么）

### 拦截流程

```
Agent 决定调用工具 (如 bash "rm -rf /")
    │
    ▼ 中间件链按序处理
   [0-3] 前置中间件处理完毕
    │
    ▼ [4] GuardrailMiddleware.wrap_tool_call(request, handler)
    │
    ├─ _build_request(request)
    │   └─ 构建 GuardrailRequest:
    │       tool_name = "bash"
    │       tool_input = {"command": "rm -rf /"}
    │       agent_id = passport (来自配置)
    │       timestamp = "2024-01-01T00:00:00Z"
    │
    ├─ provider.evaluate(guardrail_request)  ← 进入阶段 ⑥
    │
    └─ 根据决策执行阶段 ⑦
```

### 同步 vs 异步路径

| 路径 | 方法 | 调用场景 |
|------|------|---------|
| 同步 | `wrap_tool_call()` | Agent 在同步上下文中调用工具 |
| 异步 | `awrap_tool_call()` | Agent 在异步上下文中调用工具 |

两者逻辑完全一致，仅 Provider 调用方式不同（evaluate vs aevaluate）。

---

## 阶段 ⑥：评估（Provider 如何做出决策）

### 评估流程（以 AllowlistProvider 为例）

```
provider.evaluate(request)
    │
    ├─ _allowed is not None? (是否配置了白名单)
    │   ├─ 是 → request.tool_name in _allowed?
    │   │   ├─ 否 → 拒绝: GuardrailDecision(allow=False, reasons=[oap.tool_not_allowed])
    │   │   └─ 是 → 继续检查黑名单
    │   └─ 否 → 继续检查黑名单
    │
    ├─ request.tool_name in _denied?
    │   ├─ 是 → 拒绝: GuardrailDecision(allow=False, reasons=[oap.tool_not_allowed])
    │   └─ 否 → 继续
    │
    └─ 允许: GuardrailDecision(allow=True, reasons=[oap.allowed])
```

### 评估流程（以 OAP Provider 为例）

```
provider.evaluate(request)
    │
    ├─ 加载护照文件 (request.agent_id 指向的 JSON)
    │
    ├─ 检查护照状态 (status: active/suspended/revoked)
    │   └─ 非 active → 拒绝 (oap.passport_suspended)
    │
    ├─ 工具→能力映射 (bash → system.command.execute)
    │   └─ 工具对应的能力是否在 capabilities 列表中？
    │       └─ 否 → 拒绝 (oap.tool_not_allowed)
    │
    ├─ 检查命令级限制 (limits.system.command.execute)
    │   ├─ allowed_commands: 命令是否在列表中？
    │   └─ blocked_patterns: 命令是否匹配禁止模式？
    │
    └─ 返回决策
```

---

## 阶段 ⑦：决策（拒绝或放行的后续处理）

### 放行路径（allow=True）

```
decision.allow == True
    │
    ▼
return handler(request)  # 调用下一个中间件或工具本身
    │
    ▼
工具正常执行 → 返回 ToolMessage(status="success")
```

### 拒绝路径（allow=False）

```
decision.allow == False
    │
    ▼
logger.warning("Guardrail denied: tool=%s policy=%s code=%s", ...)
    │
    ▼
_build_denied_message(request, decision)
    │
    ▼
返回 ToolMessage(
    content="Guardrail denied: tool 'bash' was blocked (oap.tool_not_allowed). "
            "Reason: tool 'bash' not in allowlist. "
            "Choose an alternative approach.",
    tool_call_id="call_1",
    name="bash",
    status="error",
)
    │
    ▼
Agent 收到错误消息 → 理解拒绝原因 → 选择替代方案
```

### Provider 异常路径

```
provider.evaluate(request) 抛出异常
    │
    ├─ 是 GraphBubbleUp? → 直接抛出（LangGraph 控制流信号）
    │
    └─ 是普通 Exception?
         │
         ├─ fail_closed=True (默认)
         │   └─ logger.exception("Guardrail provider error (sync)")
         │   └─ 构建拒绝决策:
         │       GuardrailDecision(
         │           allow=False,
         │           reasons=[GuardrailReason(
         │               code="oap.evaluator_error",
         │               message="guardrail provider error (fail-closed)"
         │           )]
         │       )
         │   └─ 返回错误 ToolMessage
         │
         └─ fail_closed=False
             └─ logger.exception("Guardrail provider error (sync)")
             └─ return handler(request)  # 放行（记录警告）
```

### 空 reasons 回退

```
decision.allow == False 但 decision.reasons == []
    │
    ▼ 使用回退文本
   reason_text = "blocked by guardrail policy"
   reason_code = "oap.denied"
```

**为什么需要回退**: Provider 实现可能有 bug，返回空 reasons。回退确保 Agent 仍然收到有意义的错误消息。

---

## 生命周期总结

```
config.yaml                    ← 用户定义
    │
    ▼ 应用启动时加载一次
GuardrailsConfig (单例)         ← 全局配置
    │
    ▼ 每次创建 Agent 时
resolve_variable() → Provider 实例  ← 反射加载
    │
    ▼ 插入中间件链
GuardrailMiddleware(provider)   ← 中间件实例
    │
    ▼ 每次工具调用
_build_request() → GuardrailRequest  ← 构建评估上下文
    │
    ▼
provider.evaluate(request)      ← 策略评估
    │
    ├─ allow=True  → handler()  → 工具执行
    └─ allow=False → ToolMessage(error) → Agent 自愈
```
