"""GuardrailProvider 协议和数据结构。

定义工具调用前置授权的接口协议（Protocol），
与 OAP（Open Agent Protocol）的 Decision/Reason 对象对齐。
任何实现了 evaluate/aevaluate 方法的类都可作为 provider，
无需继承特定基类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class GuardrailRequest:
    """传递给 provider 的工具调用上下文。

    Attributes:
        tool_name: 被调用的工具名称。
        tool_input: 工具调用参数。
        agent_id: 智能体标识（可选）。
        thread_id: 线程标识（可选）。
        is_subagent: 是否来自子智能体。
        timestamp: 调用时间戳。
    """

    tool_name: str
    tool_input: dict[str, Any]
    agent_id: str | None = None
    thread_id: str | None = None
    is_subagent: bool = False
    timestamp: str = ""


@dataclass
class GuardrailReason:
    """结构化的决策原因（与 OAP reason 对象对齐）。

    Attributes:
        code: 原因代码（如 "oap.tool_not_allowed"）。
        message: 人类可读的原因描述。
    """

    code: str
    message: str = ""


@dataclass
class GuardrailDecision:
    """provider 的授权/拒绝决策（与 OAP Decision 对象对齐）。

    Attributes:
        allow: 是否允许工具调用。
        reasons: 决策原因列表。
        policy_id: 匹配的策略 ID（可选）。
        metadata: 附加元数据。
    """

    allow: bool
    reasons: list[GuardrailReason] = field(default_factory=list)
    policy_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class GuardrailProvider(Protocol):
    """可插拔的工具调用授权协议。

    任何拥有 evaluate/aevaluate 方法的类都满足此协议，
    无需继承特定基类。Provider 通过 resolve_variable() 按类路径加载，
    与 DeerFlow 的模型、工具、沙箱使用同一套反射机制。

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
