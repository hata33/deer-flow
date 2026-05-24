"""内置 Guardrail Provider，随 DeerFlow 一起发布。

当前仅包含 AllowlistProvider —— 一个零外部依赖的简单白名单/黑名单 Provider。

为什么内置 Provider 放在这个文件:
- 它们是平台的一部分，用户无需额外安装任何包即可使用
- 通过 config.yaml 中的 use 字段按类路径加载（如 deerflow.guardrails.builtin:AllowlistProvider）
- 作为自定义 Provider 的参考实现
- 测试中直接 import 用于验证 GuardrailProvider 协议兼容性

与其他 Provider 的关系:
- AllowlistProvider 是最简单的实现，仅基于工具名做匹配
- OAP Provider（第三方）在此基础上增加了护照解析、命令参数检查
- 自定义 Provider 可参考此实现的结构
"""

from deerflow.guardrails.provider import GuardrailDecision, GuardrailReason, GuardrailRequest


class AllowlistProvider:
    """基于允许/拒绝列表的简单 Provider，零外部依赖。

    支持两种模式（可组合使用）：
    - allowed_tools: 白名单模式，仅允许列表中的工具（None 表示不启用白名单）
    - denied_tools: 黑名单模式，拒绝列表中的工具（空 set 表示不启用黑名单）

    评估顺序：先检查白名单（若配置），再检查黑名单。
    若同时配置，不在白名单的工具直接拒绝，在白名单但在黑名单的也被拒绝。

    为什么 _allowed 为 None 时表示"不限制"而非"空集合":
    - None → 未配置白名单，所有工具默认允许
    - 空 set() → 空白名单，所有工具都被拒绝（配置为 allowed_tools: [] 的情况）
    这与 skills 的 allowed-tools 语义一致。

    为什么 aevaluate 直接委托给 evaluate:
    AllowlistProvider 是纯内存操作（set lookup），无 I/O 无网络，
    不需要异步实现。直接委托避免重复代码。

    Attributes:
        name: provider 名称标识，固定为 "allowlist"。
    """

    name = "allowlist"

    def __init__(self, *, allowed_tools: list[str] | None = None, denied_tools: list[str] | None = None):
        """初始化 AllowlistProvider。

        Args:
            allowed_tools: 白名单工具列表。None 表示不启用白名单（所有工具默认允许）。
                          空列表 [] 表示空白名单（所有工具都被拒绝）。
            denied_tools: 黑名单工具列表。空列表或 None 表示不启用黑名单。

        为什么使用 set 存储:
        工具名查找是 O(1) 操作，每次工具调用都要经过 Provider，
        set lookup 比 list 遍历快得多。
        """
        self._allowed = set(allowed_tools) if allowed_tools else None
        self._denied = set(denied_tools) if denied_tools else set()

    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        """同步评估工具调用是否合规。

        评估流程：
        1. 若配置了白名单且工具不在其中 → 拒绝（oap.tool_not_allowed）
        2. 若工具在黑名单中 → 拒绝（oap.tool_not_allowed）
        3. 否则 → 允许（oap.allowed）

        为什么返回的 reasons 使用 OAP 标准码:
        与 OAP 规范对齐，确保 Agent 看到的错误消息中包含标准化原因码，
        便于未来集成 OAP 兼容的监控和审计系统。
        """
        if self._allowed is not None and request.tool_name not in self._allowed:
            return GuardrailDecision(allow=False, reasons=[GuardrailReason(code="oap.tool_not_allowed", message=f"tool '{request.tool_name}' not in allowlist")])
        if request.tool_name in self._denied:
            return GuardrailDecision(allow=False, reasons=[GuardrailReason(code="oap.tool_not_allowed", message=f"tool '{request.tool_name}' is denied")])
        return GuardrailDecision(allow=True, reasons=[GuardrailReason(code="oap.allowed")])

    async def aevaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        """异步评估，直接委托给同步实现。

        为什么不需要独立的异步实现:
        AllowlistProvider 仅做内存 set 查找，无 I/O、无网络调用，
        独立的异步实现不会带来任何优势。委托给 evaluate 保持代码简洁。
        """
        return self.evaluate(request)
