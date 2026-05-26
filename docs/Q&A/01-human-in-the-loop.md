# Q&A 01: Human-in-the-loop

> Human-in-the-loop 在这个系统中的具体定义和工作流程是什么？

---

## 定义

DeerFlow 的 HITL（Human-in-the-loop）并非传统意义上的"人在环中审批"流程，而是通过两种机制实现人机协作：

1. **澄清系统** — Agent 主动向用户请求信息或确认
2. **Guardrails 授权** — 工具调用前的策略审核（自动为主，可接入人工审批）

---

## 一、澄清系统 (`ask_clarification`)

### 触发条件

系统提示词（`lead_agent/prompt.py`）强制要求 Agent 在以下场景必须先澄清：

| 场景 | clarification_type | 示例 |
|------|-------------------|------|
| 信息缺失 | `missing_info` | "帮我写爬虫" — 没说目标网站 |
| 需求模糊 | `ambiguous_requirement` | "优化代码" — 性能？可读性？内存？ |
| 方案选择 | `approach_choice` | 认证方案选 JWT、OAuth 还是 session？ |
| 风险确认 | `risk_confirmation` | 删除文件、修改生产配置 |
| 建议采纳 | `suggestion` | "建议重构这段代码，是否执行？" |

### 工作流程

```
Agent 推理 → 发现信息不足 → 调用 ask_clarification 工具
                                         ↓
                            ClarificationMiddleware 拦截
                                         ↓
                            格式化消息（图标 + 选项）
                                         ↓
                            Command(goto=END) 暂停执行
                                         ↓
                            前端渲染为 clarification 消息组
                                         ↓
                            用户回复 → LangGraph 恢复执行
```

### 关键实现

**工具定义** (`tools/builtins/clarification_tool.py`):

```python
@tool
def ask_clarification(
    question: str,
    clarification_type: str,  # missing_info | ambiguous_requirement | ...
    context: str | None = None,
    options: list[str] | None = None,
) -> str:
```

**中间件拦截** (`middlewares/clarification_middleware.py`):
- 位于中间件链最末位，确保最后执行
- 使用 `Command(goto=END)` 让 LangGraph 暂停到 END 节点
- 状态完整保留，用户回复后自动恢复

**前端渲染**:
- 消息分组类型：`assistant:clarification`
- 独立渲染组件，显示问题、选项、上下文信息

---

## 二、Guardrails 授权

### 工作流程

```
Agent 产出 tool_calls → GuardrailMiddleware 拦截
                            ↓
                    构建 GuardrailRequest
                    (tool_name, params, timestamp)
                            ↓
                    Provider.evaluate() 评估
                      ├─ allowed → 正常执行工具
                      ├─ denied → 返回错误 ToolMessage
                      └─ error → fail_closed 策略（默认拒绝）
```

### 内置 Provider

**AllowlistProvider** (`guardrails/builtin.py`):

```yaml
# config.yaml
guardrails:
  enabled: true
  provider: deerflow.guardrails.builtin:AllowlistProvider
  config:
    allowed_tools: ["bash", "read_file", "write_file"]
    denied_tools: ["rm", "dd"]
```

支持接入 OAP 兼容的外部策略服务，实现真正的人工审批。

### fail_closed 策略

当 Provider 出错（网络超时、服务不可用）时，默认**拒绝**工具调用。这是一个安全设计——宁可阻止正常操作，也不放行危险操作。

---

## 三、两套机制的协作

| 维度 | 澄清系统 | Guardrails |
|------|---------|------------|
| 触发者 | Agent（LLM 主动判断） | 系统（每次工具调用） |
| 判断依据 | 语义理解（信息是否充分） | 规则策略（工具是否被允许） |
| 用户感知 | 可见（需要用户回复） | 通常不可见（自动评估） |
| 执行位置 | 中间件链末位 | 中间件链第 4 位 |
| 实现机制 | `Command(goto=END)` 暂停图 | 工具调用前后拦截 |

---

## 相关源码

| 组件 | 文件 |
|------|------|
| 澄清工具 | `backend/packages/harness/deerflow/tools/builtins/clarification_tool.py` |
| 澄清中间件 | `backend/packages/harness/deerflow/agents/middlewares/clarification_middleware.py` |
| Guardrail 中间件 | `backend/packages/harness/deerflow/guardrails/middleware.py` |
| 内置 Provider | `backend/packages/harness/deerflow/guardrails/builtin.py` |
| 前端消息分组 | `frontend/src/core/messages/utils.ts` |

## 深入阅读

- [Agent 中间件链设计](../docs/core/agent/06-design-decisions.md)
- [Guardrails 设计决策](../docs/core/guardrails/05-design-decisions.md)
- [Agent 请求全流程](../docs/lifecycle/01-agent-request-flow.md)
