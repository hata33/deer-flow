# Guardrails 模块文件清单

## 模块概述

Guardrails 模块实现工具调用前授权机制，通过可插拔的 Provider 协议支持自定义授权策略。

## 文件清单

### 1. `/data/deer-flow-main/backend/packages/harness/deerflow/guardrails/__init__.py`

**核心导出**:
- `AllowlistProvider` - 内置白名单提供者
- `GuardrailDecision` - 授权决策
- `GuardrailMiddleware` - Guardrail 中间件
- `GuardrailProvider` - Guardrail 提供者协议
- `GuardrailReason` - 授权原因
- `GuardrailRequest` - 授权请求

**职责**: Guardrail 模块的统一导出入口

---

### 2. `/data/deer-flow-main/backend/packages/harness/deerflow/guardrails/builtin.py`

**核心类/函数**:
- `AllowlistProvider` - 简单白名单/黑名单提供者（零依赖）
  - `__init__(allowed_tools, denied_tools)` - 配置允许/拒绝的工具
  - `evaluate(request)` - 同步评估工具调用
  - `aevaluate(request)` - 异步评估工具调用

**职责**: 内置 Guardrail 提供者实现

---

### 3. `/data/deer-flow-main/backend/packages/harness/deerflow/guardrails/middleware.py`

**核心类/函数**:
- `GuardrailMiddleware` - Guardrail 中间件
  - `__init__(provider, fail_closed, passport)` - 初始化
  - `_build_request()` - 构建授权请求
  - `_build_denied_message()` - 构建拒绝消息
  - `wrap_tool_call()` / `awrap_tool_call()` - 拦截工具调用

**职责**: 在工具执行前进行授权检查

---

### 4. `/data/deer-flow-main/backend/packages/harness/deerflow/guardrails/provider.py`

**核心类/函数**:
- `GuardrailRequest` - 授权请求数据类
  - `tool_name` - 工具名称
  - `tool_input` - 工具输入
  - `agent_id` - 代理 ID
  - `thread_id` - 线程 ID
  - `is_subagent` - 是否子代理
  - `timestamp` - 时间戳
- `GuardrailReason` - 授权原因数据类
  - `code` - 原因代码
  - `message` - 原因消息
- `GuardrailDecision` - 授权决策数据类
  - `allow` - 是否允许
  - `reasons` - 原因列表
  - `policy_id` - 策略 ID
  - `metadata` - 元数据
- `GuardrailProvider` - Guardrail 提供者协议
  - `name` - 提供者名称
  - `evaluate(request)` - 同步评估
  - `aevaluate(request)` - 异步评估

**职责**: Guardrail 提供者协议和数据结构定义

---

## 工作流程

1. **配置**: 在 config.yaml 中启用 guardrails 并配置 provider
2. **拦截**: GuardrailMiddleware 拦截每个工具调用
3. **评估**: 调用 provider.evaluate() 评估是否允许
4. **决策**: 
   - 允许 → 正常执行工具
   - 拒绝 → 返回错误 ToolMessage
5. **错误处理**: 提供者异常时根据 fail_closed 决定行为
