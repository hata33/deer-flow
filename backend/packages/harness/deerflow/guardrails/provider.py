"""GuardrailProvider 协议与前置授权数据结构。

本模块定义了 Guardrails 系统的类型层：三个数据类（Request/Decision/Reason）
和一个 Protocol，它们共同构成 Provider 与 Middleware 之间的契约。

设计关键点：
1. 使用 Protocol（而非 ABC）：
   - 任何拥有 evaluate/aevaluate 方法的类都满足协议，无需显式继承
   - 通过 resolve_variable() 按类路径反射加载，与模型/工具/沙箱使用同一套机制
   - @runtime_checkable 允许 isinstance() 运行时检查

2. OAP 对齐：
   - GuardrailDecision 的字段设计与 Open Agent Passport (OAP) 规范对齐
   - GuardrailReason 使用 OAP 标准的 code/message 结构
   - 支持 policy_id 追溯匹配的策略

3. request 包含丰富上下文：
   - tool_name/tool_input：被调用的工具名和参数
   - agent_id：护照路径或托管 Agent ID（来自 guardrails_config.passport）
   - thread_id/is_subagent：线程和子 Agent 上下文（预留给高级 Provider 使用）
   - timestamp：ISO 8601 时间戳
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class GuardrailRequest:
    """每次工具调用时传递给 Provider 的评估上下文。

    包含 Provider 决策所需的全部信息：
    - tool_name: 被调用的工具名（如 'bash', 'write_file', 'web_search'）
    - tool_input: 工具调用参数（如 {'command': 'rm -rf /'}）
    - agent_id: 护照路径或托管 Agent ID，Provider 可用其加载 OAP 护照
    - thread_id: 对话线程 ID（预留给高级 Provider 实现会话级策略）
    - is_subagent: 是否为子 Agent 调用（可对子 Agent 施加更严格的限制）
    - timestamp: ISO 8601 时间戳，用于审计和时效性检查

    为什么 agent_id 可为 None:
    内置 AllowlistProvider 不需要护照，仅通过工具名做白名单/黑名单匹配。
    高级 OAP Provider 则需要 agent_id 来定位护照文件。
    """

    tool_name: str
    tool_input: dict[str, Any]
    agent_id: str | None = None
    thread_id: str | None = None
    is_subagent: bool = False
    timestamp: str = ""


@dataclass
class GuardrailReason:
    """授权/拒绝决策的结构化原因（与 OAP Reason 对象对齐）。

    每个决策可以有多个 reason（通过 GuardrailDecision.reasons 列表），
    每个 reason 包含：
    - code: 原因码，遵循 OAP 规范（如 oap.allowed, oap.tool_not_allowed）
    - message: 人类可读的描述信息，会显示在 Agent 看到的错误消息中

    常见 OAP 原因码：
    - oap.allowed / oap.denied
    - oap.tool_not_allowed
    - oap.command_not_allowed
    - oap.blocked_pattern
    - oap.limit_exceeded
    - oap.passport_suspended
    - oap.evaluator_error（Provider 自身异常时的回退码）
    """

    code: str
    message: str = ""


@dataclass
class GuardrailDecision:
    """Provider 对工具调用的授权/拒绝决策（与 OAP Decision 对象对齐）。

    核心字段：
    - allow: 是否允许执行（True=放行，False=拒绝）
    - reasons: 决策原因列表（至少一个，allow=True 时通常为 oap.allowed）
    - policy_id: 匹配的策略 ID（可选），用于审计追踪
    - metadata: 附加元数据（可选），Provider 可传递任意键值对

    当 allow=False 时，Middleware 会将 reasons[0].message 注入 ToolMessage，
    作为 Agent 看到的错误信息。
    """

    allow: bool
    reasons: list[GuardrailReason] = field(default_factory=list)
    policy_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class GuardrailProvider(Protocol):
    """可插拔工具调用授权的协议。

    任何拥有 evaluate/aevaluate 方法和 name 属性的类都满足此协议，
    无需继承特定基类。Provider 通过 resolve_variable() 按类路径加载，
    与 DeerFlow 的模型、工具、沙箱使用同一套反射机制。

    为什么用 Protocol 而非 ABC:
    - 降低耦合：Provider 实现者不需要导入 DeerFlow 的任何类型
    - 灵活性：纯 Python 类即可，不强制继承关系
    - 反射加载兼容：resolve_variable() 期望一个可调用的类，Protocol 不干预实例化

    为什么需要 name 属性:
    标识 Provider 身份，用于日志记录和调试。内置的 AllowlistProvider.name = "allowlist"。

    为什么同步和异步方法各有一个:
    AgentMiddleware 有 sync 和 async 两个 wrap_tool_call 路径，
    Provider 需要同时支持两种调用方式。简单 Provider 可在 aevaluate 中委托给 evaluate。

    Attributes:
        name: provider 的唯一名称标识。
    """

    name: str

    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        """同步评估工具调用是否应继续执行。"""
        ...

    async def aevaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        """异步评估工具调用是否应继续执行。"""
        ...
