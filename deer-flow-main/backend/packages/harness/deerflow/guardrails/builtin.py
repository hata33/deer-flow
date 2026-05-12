"""内置 Guardrail Provider。

提供零外部依赖的 AllowlistProvider，通过允许/拒绝列表控制工具调用。
适用于简单的工具访问控制场景。
"""

from deerflow.guardrails.provider import GuardrailDecision, GuardrailReason, GuardrailRequest


class AllowlistProvider:
    """基于允许/拒绝列表的简单 provider，无外部依赖。

    支持两种模式（可组合）：
    - allowed_tools: 白名单模式，仅允许列表中的工具
    - denied_tools: 黑名单模式，拒绝列表中的工具

    若同时配置，先检查白名单（不在白名单则拒绝），再检查黑名单。

    Attributes:
        name: provider 名称标识。
    """

    name = "allowlist"

    def __init__(self, *, allowed_tools: list[str] | None = None, denied_tools: list[str] | None = None):
        self._allowed = set(allowed_tools) if allowed_tools else None
        self._denied = set(denied_tools) if denied_tools else set()

    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        """评估工具调用是否合规。

        先检查白名单（若配置），再检查黑名单。
        通过则返回 allow=True。
        """
        if self._allowed is not None and request.tool_name not in self._allowed:
            return GuardrailDecision(allow=False, reasons=[GuardrailReason(code="oap.tool_not_allowed", message=f"tool '{request.tool_name}' not in allowlist")])
        if request.tool_name in self._denied:
            return GuardrailDecision(allow=False, reasons=[GuardrailReason(code="oap.tool_not_allowed", message=f"tool '{request.tool_name}' is denied")])
        return GuardrailDecision(allow=True, reasons=[GuardrailReason(code="oap.allowed")])

    async def aevaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        """异步评估，直接委托给同步实现。"""
        return self.evaluate(request)
