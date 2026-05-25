"""延迟工具过滤中间件 — 从模型绑定中移除延迟工具的 schema。

当 tool_search 启用时，MCP 工具被注册到 DeferredToolRegistry 并传递给 ToolNode
执行，但其 schema 不应通过 bind_tools 发送给 LLM（这就是延迟的核心目的 — 节省上下文 token）。

此中间件在两个层面拦截：
1. wrap_model_call：从 request.tools 中移除延迟工具，使 model.bind_tools 只接收活跃工具
2. wrap_tool_call：若模型直接调用了未提升的延迟工具，返回错误 ToolMessage

Agent 通过 tool_search 工具在运行时发现延迟工具，调用后工具 schema 被提升（promoted），
后续模型调用即可正常看到该工具。
"""

import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)


class DeferredToolFilterMiddleware(AgentMiddleware[AgentState]):
    """Remove deferred tools from request.tools before model binding.

    ToolNode still holds all tools (including deferred) for execution routing,
    but the LLM only sees active tool schemas — deferred tools are discoverable
    via tool_search at runtime.
    """

    def _filter_tools(self, request: ModelRequest) -> ModelRequest:
        from deerflow.tools.builtins.tool_search import get_deferred_registry

        registry = get_deferred_registry()
        if not registry:
            return request

        deferred_names = registry.deferred_names
        active_tools = [t for t in request.tools if getattr(
            t, "name", None) not in deferred_names]

        if len(active_tools) < len(request.tools):
            logger.debug(
                f"Filtered {len(request.tools) - len(active_tools)} deferred tool schema(s) from model binding")

        return request.override(tools=active_tools)

    def _blocked_tool_message(self, request: ToolCallRequest) -> ToolMessage | None:
        from deerflow.tools.builtins.tool_search import get_deferred_registry

        registry = get_deferred_registry()
        if not registry:
            return None

        tool_name = str(request.tool_call.get("name") or "")
        if not tool_name:
            return None

        if not registry.contains(tool_name):
            return None

        tool_call_id = str(request.tool_call.get("id")
                           or "missing_tool_call_id")
        return ToolMessage(
            content=(
                f"Error: Tool '{tool_name}' is deferred and has not been promoted yet. Call tool_search first to expose and promote this tool's schema, then retry."),
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._filter_tools(request))

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        blocked = self._blocked_tool_message(request)
        if blocked is not None:
            return blocked
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._filter_tools(request))

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        blocked = self._blocked_tool_message(request)
        if blocked is not None:
            return blocked
        return await handler(request)
