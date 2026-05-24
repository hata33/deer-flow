# Guardrails 系统全局概览

> Guardrails（护栏）是 DeerFlow 的工具调用前置授权层。每次 Agent 调用工具（bash、write_file、web_search 等）时，Guardrails 在执行前对调用进行策略评估，决定是否允许执行。

---

## 一、Guardrails 是什么

```
Guardrails = Middleware（拦截器） + Provider（策略评估器）
```

一个 Guardrails 系统本质上是一层 **Agent 中间件**，位于工具调用链路中。它不修改工具本身，而是在工具执行前插入一个"安检口"：

- **Middleware（GuardrailMiddleware）**：拦截每次工具调用，构建评估请求，调用 Provider 获取决策
- **Provider（GuardrailProvider）**：接收评估请求（工具名、参数、上下文），返回 allow/deny 决策
- **Config（GuardrailsConfig）**：控制启用/禁用、fail_closed 策略、Provider 选择

**不是沙箱的替代品，是沙箱的补充** —— 沙箱提供进程隔离（限制"怎么做"），Guardrails 提供语义授权（限制"能做什么"）。两者配合使用才能构建完整的 Agent 安全体系。

---

## 二、解决的问题

| 问题 | Guardrails 的解决方案 |
|------|----------------------|
| **Agent 可执行任意工具，无策略约束** | 每次工具调用前经过 Provider 评估，不符合策略的直接拒绝 |
| **沙箱隔离不够** | 沙箱内的 bash 仍可 curl 数据外传，Guardrails 可禁止 curl 或限制命令参数 |
| **人工审批不适合自动化流程** | 基于策略的确定性授权，无需人工介入 |
| **多租户/多 Agent 场景权限管理** | OAP 护照模式：每个 Agent 有独立的能力声明和限制 |
| **第三方需要自定义授权逻辑** | Protocol 设计：任何 Python 类实现 evaluate/aevaluate 即可 |
| **安全策略与业务逻辑耦合** | 配置驱动：通过 config.yaml 一行配置即可启用/切换 Provider |

---

## 三、能力来源全景

```
┌────────────────────────────────────────────────────────────────────┐
│                         能力来源                                    │
├──────────────────────┬──────────────────────┬──────────────────────┤
│  内置 Allowlist      │  OAP 护照 Provider   │  自定义 Provider      │
│  (builtin.py)        │  (第三方包)          │  (用户代码)           │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ 零外部依赖            │ 基于开放标准          │ 完全自定义逻辑         │
│ 工具名白名单/黑名单    │ 护照声明 + 策略评估   │ 检查参数、上下文等      │
│ 通过 config.yaml 配置  │ 支持本地/远程评估     │ 通过 config.yaml 配置  │
└──────────────────────┴──────────────────────┴──────────────────────┘
```

能力注入路径：

```
config.yaml (guardrails 配置块)
    │
    ▼ AppConfig.from_file()
   GuardrailsConfig (Pydantic 模型)
    │
    ├─► enabled=False → 跳过，不注册 GuardrailMiddleware
    │
    └─► enabled=True + provider 配置
         │
         ▼ resolve_variable(provider.use)
        Provider 类 (AllowlistProvider / OAP / 自定义)
         │
         ▼ 实例化 (传入 provider.config + framework="deerflow")
        Provider 实例
         │
         ▼ GuardrailMiddleware(provider, fail_closed, passport)
         │
         ▼ 插入中间件链第 4 位
         │
         ▼ 每次工具调用 → wrap_tool_call / awrap_tool_call
              │
              ├─ provider.evaluate(request)
              │    ├─ allow=True → 放行
              │    └─ allow=False → 返回错误 ToolMessage
              │
              └─ provider 异常
                   ├─ fail_closed=True → 阻止（返回 evaluator_error）
                   └─ fail_closed=False → 放行（记录警告）
```

---

## 四、模块架构

```
guardrails/
├── __init__.py           # 模块入口，公开接口导出
├── provider.py           # 数据类型（Request/Decision/Reason）和 Provider 协议
├── middleware.py          # GuardrailMiddleware（AgentMiddleware 子类）
└── builtin.py            # 内置 AllowlistProvider（零外部依赖）

config/
└── guardrails_config.py  # GuardrailsConfig Pydantic 模型 + 单例管理
```

**上游消费者**：

| 消费者 | 使用的模块 | 用途 |
|--------|-----------|------|
| `agents/middlewares/tool_error_handling_middleware.py` | `middleware`, `config` | 根据 guardrails_config 注册 GuardrailMiddleware |
| `agents/factory.py` | `features` | 通过 RuntimeFeatures.guardrail 控制中间件开关 |
| `config/app_config.py` | `config` | 加载 guardrails 配置到单例 |
| Gateway API | `config` | 运行时重新加载 guardrails 配置 |

---

## 五、数据流简图

```
Agent 发起工具调用 (ReAct 循环)
    │
    ▼
中间件链逐个处理
    │
    ├─ [0] ThreadDataMiddleware
    ├─ [1] UploadsMiddleware
    ├─ [2] SandboxMiddleware
    ├─ [3] DanglingToolCallMiddleware
    │
    ├─ [4] GuardrailMiddleware ◄── 拦截点
    │   │
    │   ├─ 构建 GuardrailRequest(tool_name, tool_input, agent_id, timestamp)
    │   │
    │   ├─ provider.evaluate(request)
    │   │   ├─ AllowlistProvider: set lookup → 白名单/黑名单匹配
    │   │   └─ OAP Provider: 护照解析 → 能力检查 → 限制匹配
    │   │
    │   ├─ allow=True → 放行
    │   │   └─ 调用 handler(request) → 工具执行
    │   │
    │   └─ allow=False → 拒绝
    │       └─ 返回 ToolMessage(status="error", content="Guardrail denied: ...")
    │           └─ Agent 看到错误 → 选择替代方案
    │
    ├─ [5] ToolErrorHandlingMiddleware
    ├─ [6+] 业务中间件 (Summarization, Title, Memory, ...)
    │
    ▼
工具实际执行
```

---

## 六、与其他安全机制的关系

```
                    ┌──────────────────┐
                    │   Agent 工具调用   │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌──────────┐  ┌──────────┐  ┌──────────────┐
        │ 沙箱隔离  │  │ Guardrails│  │ 人工审批       │
        │ 进程级    │  │ 策略级    │  │ (ask_clari-  │
        │ 文件系统  │  │ 工具/参数 │  │  fication)    │
        │ 网络隔离  │  │ 级别授权  │  │ 逐次确认      │
        └──────────┘  └──────────┘  └──────────────┘
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                    ┌────────────────┐
                    │  多层安全防护    │
                    │  纵深防御体系    │
                    └────────────────┘
```

- **沙箱隔离** 回答"在什么环境中执行"（进程隔离、文件系统隔离、网络隔离）
- **Guardrails** 回答"能否执行"（基于策略的工具/参数级别授权）
- **人工审批** 回答"是否确认执行"（高风险操作的最终人工决策）

三者构成纵深防御：
1. 沙箱是第一道防线（限制攻击面）
2. Guardrails 是第二道防线（阻止未授权操作）
3. 人工审批是第三道防线（高风险兜底）

---

## 七、关键设计原则

| 原则 | 说明 |
|------|------|
| **安全优先（fail-closed）** | Provider 异常时默认阻止调用，宁可误杀不可放过 |
| **可插拔 Provider** | Protocol 定义接口，内置/第三方/自定义 Provider 自由切换 |
| **Agent 可自愈** | 拒绝时返回 ToolMessage（status=error），Agent 看到原因后可选择替代方案 |
| **配置驱动** | config.yaml 一行配置即可启用/切换/禁用，无需改代码 |
| **零依赖起点** | 内置 AllowlistProvider 零外部依赖，开箱即用 |
| **开放标准对齐** | 数据类型（Decision/Reason）与 OAP 规范对齐，兼容第三方 Provider |
| **GraphBubbleUp 透传** | LangGraph 控制流信号（interrupt/pause/resume）不被捕获 |
