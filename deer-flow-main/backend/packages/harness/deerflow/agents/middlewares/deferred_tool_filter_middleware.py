"""用于从模型绑定中过滤延迟工具模式的中间件。

当启用 tool_search 时，MCP 工具注册到 DeferredToolRegistry 并传递给 ToolNode 执行，
但其模式不应通过 bind_tools 发送给 LLM（这就是延迟的全部意义——节省上下文 token）。

此中间件拦截 wrap_model_call 并从 request.tools 中移除延迟工具，
使 model.bind_tools 只接收活跃的工具模式。
智能体在运行时通过 tool_search 工具发现延迟工具。
"""

import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse

logger = logging.getLogger(__name__)


class DeferredToolFilterMiddleware(AgentMiddleware[AgentState]):
    """在模型绑定前从 request.tools 中移除延迟工具。

    ToolNode 仍持有所有工具（包括延迟工具）用于执行路由，
    但 LLM 只能看到活跃的工具模式——延迟工具可在运行时通过 tool_search 发现。
    """

    def _filter_tools(self, request: ModelRequest) -> ModelRequest:
        from deerflow.tools.builtins.tool_search import get_deferred_registry

        registry = get_deferred_registry()
        if not registry:
            return request

        deferred_names = {e.name for e in registry.entries}
        active_tools = [t for t in request.tools if getattr(t, "name", None) not in deferred_names]

        if len(active_tools) < len(request.tools):
            logger.debug(f"Filtered {len(request.tools) - len(active_tools)} deferred tool schema(s) from model binding")

        return request.override(tools=active_tools)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._filter_tools(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._filter_tools(request))
