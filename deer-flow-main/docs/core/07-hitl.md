# Human-in-the-Loop 人机介入——底层逻辑与本质

## 一句话本质

HITL = **Agent 主动暂停执行，向人类请求输入**。不是被动等待人类指令，而是 Agent 在执行过程中识别到"我需要人类决策"的时刻，主动中断并等待回复。

---

## 1. 澄清机制——Agent 说"等等，我不确定"

### 工具是信号，中间件是执行者

```python
# 工具定义（LLM 可以调用的接口）
@tool(return_direct=True)
def ask_clarification(question, clarification_type, context=None, options=None):
    return "Clarification requested"   # ← 函数体是占位符

# 中间件拦截（真正的行为实现）
class ClarificationMiddleware:
    def wrap_tool_call(self, tool_call):
        if tool_name == "ask_clarification":
            # 不执行工具，而是中断执行
            return Command(
                update={"messages": [formatted_question]},
                goto=END    # ← 跳转到图的终点，暂停执行
            )
```

### 五种澄清场景

| 类型 | 场景 | 示例 |
|------|------|------|
| `missing_info` | 缺少必要信息 | "部署应用"但没说哪个环境 |
| `ambiguous_requirement` | 存在多种理解 | "优化代码"是性能还是可读性？ |
| `approach_choice` | 多种技术路线 | 认证用 JWT 还是 OAuth？ |
| `risk_confirmation` | 危险操作确认 | 删除生产数据库 |
| `suggestion` | 方案需审批 | "建议重构这个模块，是否继续？" |

### 执行流程

```
LLM 判断需要澄清
  │
  ▼
调用 ask_clarification(question="部署到哪个环境?", options=["dev","staging","prod"])
  │
  ▼
ClarificationMiddleware 拦截
  │
  ├─ 格式化问题为用户友好的文本
  ├─ 创建 ToolMessage 包含格式化后的问题
  └─ 返回 Command(goto=END) ← 执行中断，用户看到问题
  │
  ▼
用户回复: "staging"
  │
  ▼
LangGraph 从断点恢复执行
  │
  ▼
LLM 基于用户回复继续处理
```

**核心启示**：HITL 不是"人类监督 Agent"，而是"Agent 在执行中识别不确定性并主动暂停"。关键设计是把"暂停"实现为 LangGraph 图的状态跳转（`goto=END`），不是抛异常或进程挂起。图执行被干净地终止，状态被检查点持久化，用户回复后从断点无缝恢复。这比传统的"人类审批队列"模式优雅得多——不需要外部状态管理，图本身就是一个可中断、可恢复的状态机。

## 2. 循环检测——系统替人类做"停"的决策

不是所有 HITL 都需要人类显式参与。当 Agent 陷入循环时，系统自动介入：

```
重复 3 次 → 注入 HumanMessage（伪装成用户说的）：
  "[LOOP DETECTED] 你正在重复相同的工具调用。
   停止调用工具，直接给出最终答案。"

重复 5 次 → 强制清空 tool_calls：
  Agent 无法调用任何工具，只能输出文本响应
```

**为什么用 HumanMessage 而不是 SystemMessage？** Anthropic 的 Claude API 对 SystemMessage 有特殊限制——非连续的 SystemMessage 会导致错误。用 HumanMessage 伪装成"用户的制止指令"，兼容所有 LLM 提供商。

**核心启示**：HITL 不一定需要人类实时在线。有些情况（循环、超时、资源超限）系统可以代替人类做"停下来"的决策。设计时区分"必须人类判断的情况"（澄清）和"系统可以自动处理的情况"（循环检测），前者暂停等待输入，后者自动纠偏。

## 3. Guardrail 中间件——工具调用的安全门控

```python
class GuardrailMiddleware:
    def wrap_tool_call(self, tool_call):
        # 评估工具调用是否被允许
        decision = guardrail_provider.evaluate(
            tool_name=tool_call.name,
            tool_input=tool_call.args,
            agent_id=...,
            thread_id=...,
        )

        if decision.allowed:
            return tool_call()        # 放行
        else:
            return ToolMessage(       # 拒绝，返回错误消息
                content=f"Error: {decision.reason}",
                status="error"
            )
```

### 失败安全策略

```python
# 提供者抛异常时的行为
if fail_closed:
    return error_message   # 默认：拒绝调用（安全优先）
else:
    return tool_call()     # 放行（可用性优先）
```

**核心启示**：Guardrail 是"前置审批"模式——在工具执行之前做策略评估，而不是在执行之后做审计。`fail_closed` 默认值很重要：当授权服务不可用时，宁可拒绝所有调用（安全），也不要放行所有调用（方便）。这对生产系统至关重要——授权服务的不可用不应该变成权限的全面失效。

## 4. 三层 HITL 架构——从预防到纠正到兜底

```
预防层：GuardrailMiddleware（工具调用前）
  │  拦截不授权的工具调用，返回错误消息
  │  LLM 看到错误后可以换一种方式完成任务
  │
  ▼
纠正层：ClarificationMiddleware（LLM 决策后）
  │  Agent 识别到不确定性，主动暂停请求人类输入
  │  用户回复后从断点恢复
  │
  ▼
兜底层：LoopDetectionMiddleware（重复行为检测）
     Agent 陷入循环，系统自动介入打断
     两级响应：先警告、后硬停
```

三层共同保证：Agent 不会在人类不知情的情况下执行危险操作（Guardrail），不会在不确定时自作主张（Clarification），不会因为 LLM 的固有限限而无限循环（Loop Detection）。

**核心启示**：HITL 不是单一机制，而是纵深防御体系。每一层解决不同的问题：
- **Guardrail**：解决"Agent 不应该做 X 但 LLM 决定做 X"
- **Clarification**：解决"Agent 遇到不确定但选择猜测而不是询问"
- **Loop Detection**：解决"Agent 反复尝试同一无效操作"

三层独立生效，即使某一层被绕过或失效，其他层仍能保护。这和 Agent 系统中子智能体并发的"提示词 + 思维引导 + 中间件截断"三重约束是同一设计模式——**安全约束永远不要只靠一层**。
