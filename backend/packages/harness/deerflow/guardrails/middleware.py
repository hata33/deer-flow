"""GuardrailMiddleware —— 工具调用前置授权中间件。

每次工具调用在执行前都要经过此中间件的评估。它是 AgentMiddleware 的子类，
通过重写 wrap_tool_call / awrap_tool_call 方法在工具执行前插入授权检查。

中间件链中的位置：
  0-2. Sandbox 基础设施（ThreadData → Uploads → Sandbox）
  3.   DanglingToolCallMiddleware
  4.   GuardrailMiddleware  ◄── 本中间件
  5.   ToolErrorHandlingMiddleware
  6+.  (Summarization, Title, Memory, Vision, Subagent, LoopDetection, Clarify)

为什么放在第 4 位:
- 在沙箱初始化之后（此时工具调用上下文已完整构建）
- 在 ToolErrorHandling 之前（拒绝消息也需要被 ToolErrorHandling 兜底）
- 在业务中间件之前（Summarization/Memory 等）

设计关键点：
1. fail_closed 安全策略：Provider 异常时默认拒绝调用（安全优先）
2. Agent 可自愈：拒绝时返回 ToolMessage(status="error")，Agent 可看到原因并选择替代方案
3. GraphBubbleUp 透传：LangGraph 控制流信号（interrupt/pause）不被捕获，直接向上传播
4. 同步+异步双路径：同时覆盖 sync 和 async 工具调用场景
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.guardrails.provider import GuardrailDecision, GuardrailProvider, GuardrailReason, GuardrailRequest

logger = logging.getLogger(__name__)


class GuardrailMiddleware(AgentMiddleware[AgentState]):
    """工具调用前置授权中间件。

    在工具执行前通过 GuardrailProvider 评估调用合规性。
    被拒绝的调用返回错误 ToolMessage，包含拒绝原因码和替代建议。
    Provider 异常时遵循 fail_closed 策略：默认阻止调用（安全优先）。

    工作流程：
    1. 从 ToolCallRequest 构建 GuardrailRequest（提取工具名、参数、时间戳等）
    2. 调用 provider.evaluate(request) 获取决策
    3. 若 allow=True → 放行，调用 handler(request) 执行工具
    4. 若 allow=False → 构建错误 ToolMessage 返回给 Agent
    5. 若 Provider 异常：
       - fail_closed=True（默认）→ 阻止调用，返回 evaluator_error
       - fail_closed=False → 放行，记录警告
    6. GraphBubbleUp 异常（LangGraph 控制信号）→ 直接透传，不捕获

    Attributes:
        provider: 授权策略 Provider 实例（AllowlistProvider / OAP Provider / 自定义）
        fail_closed: Provider 异常时是否阻止调用（默认 True，安全优先）
        passport: 传递给 Provider 的 Agent 标识（护照路径或托管 Agent ID）
    """

    def __init__(self, provider: GuardrailProvider, *, fail_closed: bool = True, passport: str | None = None):
        """初始化 GuardrailMiddleware。

        Args:
            provider: GuardrailProvider 实例（通过 resolve_variable() 动态加载）
            fail_closed: Provider 异常时是阻止（True）还是放行（False）
            passport: 传递给 Provider 的 Agent 标识，Provider 通过 request.agent_id 获取

        为什么默认 fail_closed=True:
        安全系统的默认策略应该是"不确定就拒绝"，而不是"不确定就放行"。
        这避免了 Provider 崩溃时 Agent 获得不受限制的工具访问权。
        """
        self.provider = provider
        self.fail_closed = fail_closed
        self.passport = passport

    def _build_request(self, request: ToolCallRequest) -> GuardrailRequest:
        """从工具调用请求构建 GuardrailRequest。

        提取 tool_name（默认空字符串）和 tool_input（默认空字典），
        注入 passport 作为 agent_id，并打上 UTC 时间戳。
        线程 ID 和子 Agent 标识在此版本中保留默认值，预留给高级 Provider。
        """
        return GuardrailRequest(
            tool_name=str(request.tool_call.get("name", "")),
            tool_input=request.tool_call.get("args", {}),
            agent_id=self.passport,
            timestamp=datetime.now(UTC).isoformat(),
        )

    def _build_denied_message(self, request: ToolCallRequest, decision: GuardrailDecision) -> ToolMessage:
        """构建拒绝时的错误 ToolMessage。

        从 decision.reasons[0] 提取原因码和消息，注入到 ToolMessage 中。
        ToolMessage 的 status="error" 告诉 Agent 工具调用失败，
        content 中包含拒绝原因和替代建议（"Choose an alternative approach"），
        引导 Agent 尝试其他方式完成任务。

        为什么显示原因码:
        OAP 原因码（如 oap.tool_not_allowed）帮助 Agent 理解被拒绝的具体原因，
        从而做出更精准的替代选择。
        """
        tool_name = str(request.tool_call.get("name", "unknown_tool"))
        tool_call_id = str(request.tool_call.get("id", "missing_id"))
        reason_text = decision.reasons[0].message if decision.reasons else "blocked by guardrail policy"
        reason_code = decision.reasons[0].code if decision.reasons else "oap.denied"
        return ToolMessage(
            content=f"Guardrail denied: tool '{tool_name}' was blocked ({reason_code}). Reason: {reason_text}. Choose an alternative approach.",
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """同步拦截工具调用，评估合规性后决定放行或拒绝。

        流程：
        1. 构建 GuardrailRequest
        2. 调用 provider.evaluate()（同步）
        3. GraphBubbleUp → 直接抛出（保留 LangGraph 控制流）
        4. 其他异常 → 按 fail_closed 策略处理
        5. allow=True → 调用 handler(request) 放行
        6. allow=False → 返回错误 ToolMessage

        为什么 GraphBubbleUp 必须直接抛出:
        GraphBubbleUp 是 LangGraph 用于 interrupt/pause/resume 的控制信号，
        如果被捕获并当作普通异常处理，会导致工作流暂停机制失效。
        """
        gr = self._build_request(request)
        try:
            decision = self.provider.evaluate(gr)
        except GraphBubbleUp:
            # Preserve LangGraph control-flow signals (interrupt/pause/resume).
            raise
        except Exception:
            logger.exception("Guardrail provider error (sync)")
            if self.fail_closed:
                decision = GuardrailDecision(allow=False, reasons=[GuardrailReason(
                    code="oap.evaluator_error", message="guardrail provider error (fail-closed)")])
            else:
                return handler(request)
        if not decision.allow:
            logger.warning("Guardrail denied: tool=%s policy=%s code=%s", gr.tool_name,
                           decision.policy_id, decision.reasons[0].code if decision.reasons else "unknown")
            return self._build_denied_message(request, decision)
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """异步拦截工具调用，评估合规性后决定放行或拒绝。

        与同步版本的逻辑完全一致，区别在于：
        - 调用 provider.aevaluate() 而非 evaluate()
        - handler 是 async callable，需要 await

        为什么需要独立的异步版本:
        AgentMiddleware 框架根据调用上下文分别调用 sync/async 路径，
        Provider 可能依赖异步 I/O（如 OAP Provider 做网络请求），
        因此必须支持异步评估。
        """
        gr = self._build_request(request)
        try:
            decision = await self.provider.aevaluate(gr)
        except GraphBubbleUp:
            # Preserve LangGraph control-flow signals (interrupt/pause/resume).
            raise
        except Exception:
            logger.exception("Guardrail provider error (async)")
            if self.fail_closed:
                decision = GuardrailDecision(allow=False, reasons=[GuardrailReason(
                    code="oap.evaluator_error", message="guardrail provider error (fail-closed)")])
            else:
                return await handler(request)
        if not decision.allow:
            logger.warning("Guardrail denied: tool=%s policy=%s code=%s", gr.tool_name,
                           decision.policy_id, decision.reasons[0].code if decision.reasons else "unknown")
            return self._build_denied_message(request, decision)
        return await handler(request)
