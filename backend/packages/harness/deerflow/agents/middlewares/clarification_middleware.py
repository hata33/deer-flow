"""澄清请求拦截中间件 — 拦截 ask_clarification 工具调用并中断执行。

当模型调用 ask_clarification 工具时，此中间件：
1. 拦截工具调用，阻止其执行
2. 提取澄清问题及元数据（类型、上下文、选项）
3. 格式化为用户友好的消息（带类型图标）
4. 返回 Command 中断执行，向用户展示问题
5. 等待用户回复后继续执行

设计理由：
  澄清是 Agent 与用户的交互行为，不应作为普通工具调用执行。
  拦截后直接中断图执行（goto=END），用户回复时 LangGraph 自动恢复。

消息格式化：
  - 缺失信息（missing_info）：❓
  - 需求模糊（ambiguous_requirement）：🤔
  - 方案选择（approach_choice）：🔀
  - 风险确认（risk_confirmation）：⚠️
  - 建议（suggestion）：💡

消息 ID 稳定性：
  使用 _stable_message_id() 生成确定性 ID，
  确保重试的澄清调用替换而非追加消息。

兼容性：
  - 某些模型（如 Qwen3-Max）将数组参数序列化为 JSON 字符串，
    _format_clarification_message() 中做了反序列化处理
"""

import json
import logging
from collections.abc import Callable
from hashlib import sha256
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.graph import END
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)


class ClarificationMiddlewareState(AgentState):
    """Compatible with the `ThreadState` schema."""

    pass


class ClarificationMiddleware(AgentMiddleware[ClarificationMiddlewareState]):
    """Intercepts clarification tool calls and interrupts execution to present questions to the user.

    When the model calls the `ask_clarification` tool, this middleware:
    1. Intercepts the tool call before execution
    2. Extracts the clarification question and metadata
    3. Formats a user-friendly message
    4. Returns a Command that interrupts execution and presents the question
    5. Waits for user response before continuing

    This replaces the tool-based approach where clarification continued the conversation flow.
    """

    state_schema = ClarificationMiddlewareState

    def _stable_message_id(self, tool_call_id: str, formatted_message: str) -> str:
        """Build a deterministic message ID so retried clarification calls replace, not append."""
        if tool_call_id:
            return f"clarification:{tool_call_id}"
        digest = sha256(formatted_message.encode("utf-8")).hexdigest()[:16]
        return f"clarification:{digest}"

    def _is_chinese(self, text: str) -> bool:
        """Check if text contains Chinese characters.

        Args:
            text: Text to check

        Returns:
            True if text contains Chinese characters
        """
        return any("\u4e00" <= char <= "\u9fff" for char in text)

    def _format_clarification_message(self, args: dict) -> str:
        """Format the clarification arguments into a user-friendly message.

        Args:
            args: The tool call arguments containing clarification details

        Returns:
            Formatted message string
        """
        question = args.get("question", "")
        clarification_type = args.get("clarification_type", "missing_info")
        context = args.get("context")
        options = args.get("options", [])

        # Some models (e.g. Qwen3-Max) serialize array parameters as JSON strings
        # instead of native arrays. Deserialize and normalize so `options`
        # is always a list for the rendering logic below.
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except (json.JSONDecodeError, TypeError):
                options = [options]

        if options is None:
            options = []
        elif not isinstance(options, list):
            options = [options]

        # Type-specific icons
        type_icons = {
            "missing_info": "❓",
            "ambiguous_requirement": "🤔",
            "approach_choice": "🔀",
            "risk_confirmation": "⚠️",
            "suggestion": "💡",
        }

        icon = type_icons.get(clarification_type, "❓")

        # Build the message naturally
        message_parts = []

        # Add icon and question together for a more natural flow
        if context:
            # If there's context, present it first as background
            message_parts.append(f"{icon} {context}")
            message_parts.append(f"\n{question}")
        else:
            # Just the question with icon
            message_parts.append(f"{icon} {question}")

        # Add options in a cleaner format
        if options and len(options) > 0:
            message_parts.append("")  # blank line for spacing
            for i, option in enumerate(options, 1):
                message_parts.append(f"  {i}. {option}")

        return "\n".join(message_parts)

    def _handle_clarification(self, request: ToolCallRequest) -> Command:
        """Handle clarification request and return command to interrupt execution.

        Args:
            request: Tool call request

        Returns:
            Command that interrupts execution with the formatted clarification message
        """
        # Extract clarification arguments
        args = request.tool_call.get("args", {})
        question = args.get("question", "")

        logger.info("Intercepted clarification request")
        logger.debug("Clarification question: %s", question)

        # Format the clarification message
        formatted_message = self._format_clarification_message(args)

        # Get the tool call ID
        tool_call_id = request.tool_call.get("id", "")

        # Create a ToolMessage with the formatted question
        # This will be added to the message history
        tool_message = ToolMessage(
            id=self._stable_message_id(tool_call_id, formatted_message),
            content=formatted_message,
            tool_call_id=tool_call_id,
            name="ask_clarification",
        )

        # Return a Command that:
        # 1. Adds the formatted tool message
        # 2. Interrupts execution by going to __end__
        # Note: We don't add an extra AIMessage here - the frontend will detect
        # and display ask_clarification tool messages directly
        return Command(
            update={"messages": [tool_message]},
            goto=END,
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Intercept ask_clarification tool calls and interrupt execution (sync version).

        Args:
            request: Tool call request
            handler: Original tool execution handler

        Returns:
            Command that interrupts execution with the formatted clarification message
        """
        # Check if this is an ask_clarification tool call
        if request.tool_call.get("name") != "ask_clarification":
            # Not a clarification call, execute normally
            return handler(request)

        return self._handle_clarification(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Intercept ask_clarification tool calls and interrupt execution (async version).

        Args:
            request: Tool call request
            handler: Original tool execution handler (async)

        Returns:
            Command that interrupts execution with the formatted clarification message
        """
        # Check if this is an ask_clarification tool call
        if request.tool_call.get("name") != "ask_clarification":
            # Not a clarification call, execute normally
            return await handler(request)

        return self._handle_clarification(request)
