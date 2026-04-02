"""
中间件系统

提供请求/响应处理管道
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable

from langchain_core.messages import BaseMessage

from .state import AgentState

logger = logging.getLogger(__name__)


class Middleware(ABC):
    """
    中间件抽象基类

    中间件可以在请求处理的不同阶段介入
    """

    @property
    def name(self) -> str:
        """中间件名称"""
        return self.__class__.__name__

    async def before_request(
        self,
        state: AgentState,
        input_text: str,
    ) -> str | None:
        """
        请求前处理

        Args:
            state: 当前状态
            input_text: 用户输入

        Returns:
            修改后的输入，None 表示不修改
        """
        return None

    async def after_response(
        self,
        state: AgentState,
        response: str,
    ) -> str | None:
        """
        响应后处理

        Args:
            state: 当前状态
            response: 模型响应

        Returns:
            修改后的响应，None 表示不修改
        """
        return None

    async def before_tool_call(
        self,
        state: AgentState,
        tool_name: str,
        tool_args: dict,
    ) -> tuple[str, dict] | None:
        """
        工具调用前处理

        Args:
            state: 当前状态
            tool_name: 工具名称
            tool_args: 工具参数

        Returns:
            (修改后的工具名, 修改后的参数)，None 表示不修改
        """
        return None

    async def after_tool_call(
        self,
        state: AgentState,
        tool_name: str,
        tool_result: Any,
    ) -> Any:
        """
        工具调用后处理

        Args:
            state: 当前状态
            tool_name: 工具名称
            tool_result: 工具执行结果

        Returns:
            修改后的结果，None 表示不修改
        """
        return None


class MiddlewareChain:
    """
    中间件链

    按顺序执行多个中间件
    """

    def __init__(self, middlewares: list[Middleware] | None = None):
        self.middlewares = middlewares or []

    def add(self, middleware: Middleware) -> None:
        """添加中间件"""
        self.middlewares.append(middleware)
        logger.debug(f"添加中间件: {middleware.name}")

    async def process_before_request(
        self,
        state: AgentState,
        input_text: str,
    ) -> str:
        """处理请求前阶段"""
        result = input_text
        for middleware in self.middlewares:
            output = await middleware.before_request(state, result)
            if output is not None:
                result = output
        return result

    async def process_after_response(
        self,
        state: AgentState,
        response: str,
    ) -> str:
        """处理响应后阶段"""
        result = response
        for middleware in self.middlewares:
            output = await middleware.after_response(state, result)
            if output is not None:
                result = output
        return result


# ============================================================================
# 内置中间件
# ============================================================================

class LoggingMiddleware(Middleware):
    """日志中间件"""

    async def before_request(self, state: AgentState, input_text: str) -> str | None:
        logger.info(f"[请求] {input_text[:100]}...")
        return None

    async def after_response(self, state: AgentState, response: str) -> str | None:
        logger.info(f"[响应] {response[:100]}...")
        return None


class ContextMiddleware(Middleware):
    """上下文中间件 - 注入额外上下文"""

    def __init__(self, context_data: dict[str, Any]):
        self.context_data = context_data

    async def before_request(self, state: AgentState, input_text: str) -> str | None:
        # 将上下文数据注入到状态中
        state.context.update(self.context_data)
        return None


class TruncationMiddleware(Middleware):
    """截断中间件 - 限制输入长度"""

    def __init__(self, max_length: int = 10000):
        self.max_length = max_length

    async def before_request(self, state: AgentState, input_text: str) -> str | None:
        if len(input_text) > self.max_length:
            logger.warning(f"输入过长，截断到 {self.max_length} 字符")
            return input_text[:self.max_length] + "\n[内容已截断]"
        return None
