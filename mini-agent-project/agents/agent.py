"""
Mini Agent 主代理

简化版的 AI 代理实现
"""
import logging
from typing import Any

from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    SystemMessage,
    ToolMessage,
)

from config import get_app_config
from models import create_chat_model
from tools import get_registered_tools
from .state import AgentState
from .middlewares import MiddlewareChain, LoggingMiddleware

logger = logging.getLogger(__name__)


class MiniAgent:
    """
    Mini Agent 主代理类

    提供对话、工具调用、状态管理等功能
    """

    def __init__(
        self,
        model_name: str | None = None,
        middlewares: list | None = None,
        system_prompt: str | None = None,
    ):
        """
        初始化代理

        Args:
            model_name: 模型名称
            middlewares: 中间件列表
            system_prompt: 系统提示词
        """
        self.config = get_app_config()
        self.model_name = model_name or self.config.default_model
        self.system_prompt = system_prompt or self._default_system_prompt()

        # 创建模型
        self.model = create_chat_model(self.model_name)

        # 初始化中间件
        self.middleware_chain = MiddlewareChain(middlewares or [])
        self.middleware_chain.add(LoggingMiddleware())

        # 获取工具
        self.tools = get_registered_tools()

        logger.info(f"MiniAgent 初始化完成，模型: {self.model_name}")

    def _default_system_prompt(self) -> str:
        """默认系统提示词"""
        return """你是一个有用的 AI 助手，名为 Mini Agent。

你可以使用工具来帮助用户完成任务：
- bash: 执行命令
- read_file: 读取文件
- write_file: 写入文件
- list_dir: 列出目录

请根据用户需求选择合适的工具，并以友好的方式回复。
"""

    async def chat(
        self,
        message: str,
        state: AgentState | None = None,
    ) -> tuple[str, AgentState]:
        """
        与代理对话

        Args:
            message: 用户消息
            state: 当前状态，如果为 None 则创建新状态

        Returns:
            (响应文本, 更新后的状态)
        """
        # 创建或获取状态
        if state is None:
            state = AgentState()

        state.input = message

        # 中间件：请求前处理
        processed_input = await self.middleware_chain.process_before_request(
            state, message
        )

        # 使用处理后的输入
        input_to_use = processed_input if processed_input else message

        # 构建消息列表（不包含状态中的历史消息，避免重复）
        messages = []

        # 添加系统提示词
        if self.system_prompt:
            messages.append(SystemMessage(content=self.system_prompt))

        # 添加历史消息（排除当前输入，避免重复）
        for msg in state.messages:
            # 跳过 HumanMessage，因为我们即将添加新的输入
            if not isinstance(msg, HumanMessage):
                messages.append(msg)

        # 添加用户输入
        messages.append(HumanMessage(content=input_to_use))

        # 更新状态中的用户消息
        state.add_message(HumanMessage(content=message))

        try:
            # 调用模型
            if self.tools:
                # 绑定工具
                model_with_tools = self.model.bind_tools(self.tools)
                response = await model_with_tools.ainvoke(messages)
            else:
                response = await self.model.ainvoke(messages)

            # 处理响应
            if isinstance(response, AIMessage):
                # 检查是否有工具调用
                if hasattr(response, 'tool_calls') and response.tool_calls:
                    # 执行工具调用
                    tool_messages = await self._execute_tool_calls(
                        state, response.tool_calls
                    )

                    # 添加 AI 的工具调用消息
                    state.add_message(response)

                    # 添加工具结果消息
                    for tool_msg in tool_messages:
                        state.add_message(tool_msg)
                        messages.append(tool_msg)

                    # 再次调用模型获取最终响应
                    final_response = await self.model.ainvoke(messages)
                    response_text = final_response.content if hasattr(final_response, 'content') else str(final_response)
                else:
                    response_text = response.content if hasattr(response, 'content') else str(response)
            else:
                response_text = str(response)

            # 添加 AI 响应到状态
            state.add_message(AIMessage(content=response_text))
            state.output = response_text

            # 中间件：响应后处理
            processed_output = await self.middleware_chain.process_after_response(
                state, response_text
            )

            if processed_output and processed_output != response_text:
                state.output = processed_output
                response_text = processed_output

            return response_text, state

        except Exception as e:
            logger.error(f"对话处理错误: {e}", exc_info=True)
            error_msg = f"处理请求时出错: {e}"
            state.add_message(AIMessage(content=error_msg))
            state.output = error_msg
            return error_msg, state

    async def _execute_tool_calls(
        self,
        state: AgentState,
        tool_calls: list,
    ) -> list[ToolMessage]:
        """
        执行工具调用

        Args:
            state: 当前状态
            tool_calls: 工具调用列表

        Returns:
            ToolMessage 列表
        """
        tool_messages = []

        for tool_call in tool_calls:
            tool_name = tool_call.get('name', 'unknown')
            tool_args = tool_call.get('args', {})
            tool_id = tool_call.get('id', '')

            logger.info(f"执行工具: {tool_name} 参数: {tool_args}")

            try:
                # 查找工具
                tool = next((t for t in self.tools if t.name == tool_name), None)

                if tool is None:
                    result = f"错误: 未找到工具 '{tool_name}'"
                else:
                    # 执行工具
                    result = await tool.ainvoke(tool_args)
                    logger.info(f"工具 {tool_name} 执行成功")

            except Exception as e:
                error_msg = f"工具 {tool_name} 执行错误: {e}"
                logger.error(error_msg)
                result = error_msg

            # 创建 ToolMessage
            tool_message = ToolMessage(
                content=str(result),
                tool_call_id=tool_id,
                name=tool_name,
            )
            tool_messages.append(tool_message)

        return tool_messages

    def get_state(self) -> dict[str, Any]:
        """获取代理状态信息"""
        return {
            "model_name": self.model_name,
            "tools_count": len(self.tools),
            "middlewares": [m.name for m in self.middleware_chain.middlewares],
        }

    def reset(self) -> None:
        """重置代理状态"""
        logger.info("重置代理状态")


# ============================================================================
# 便捷函数
# ============================================================================

async def create_agent(
    model_name: str | None = None,
    system_prompt: str | None = None,
) -> MiniAgent:
    """
    创建代理实例

    Args:
        model_name: 模型名称
        system_prompt: 系统提示词

    Returns:
        MiniAgent 实例
    """
    return MiniAgent(
        model_name=model_name,
        system_prompt=system_prompt,
    )
