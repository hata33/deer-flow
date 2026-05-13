"""在 LLM 调用前将图片详情注入对话的中间件。"""

import logging
from typing import NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import ViewedImageData

logger = logging.getLogger(__name__)


class ViewImageMiddlewareState(AgentState):
    """与 `ThreadState` 模式兼容。"""

    viewed_images: NotRequired[dict[str, ViewedImageData] | None]


class ViewImageMiddleware(AgentMiddleware[ViewImageMiddlewareState]):
    """在 view_image 工具完成后将图片详情作为 human 消息注入 LLM 调用之前。

    此中间件：
    1. 在每次 LLM 调用前运行
    2. 检查最后一条助手消息是否包含 view_image 工具调用
    3. 验证该消息中的所有工具调用已完成（有对应的 ToolMessage）
    4. 如果条件满足，创建包含所有已查看图片详情（包括 base64 数据）的 human 消息
    5. 将消息添加到状态中，使 LLM 可以查看和分析图片

    这使 LLM 能够自动接收和分析通过 view_image 工具加载的图片，
    而不需要用户显式提示来描述图片。
    """

    state_schema = ViewImageMiddlewareState

    def _get_last_assistant_message(self, messages: list) -> AIMessage | None:
        """从消息列表中获取最后一条助手消息。

        参数：
            messages: 消息列表

        返回：
            最后一条 AIMessage，如果未找到则返回 None
        """
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                return msg
        return None

    def _has_view_image_tool(self, message: AIMessage) -> bool:
        """检查助手消息是否包含 view_image 工具调用。

        参数：
            message: 要检查的助手消息

        返回：
            如果消息包含 view_image 工具调用则返回 True
        """
        if not hasattr(message, "tool_calls") or not message.tool_calls:
            return False

        return any(tool_call.get("name") == "view_image" for tool_call in message.tool_calls)

    def _all_tools_completed(self, messages: list, assistant_msg: AIMessage) -> bool:
        """检查助手消息中的所有工具调用是否已完成。

        参数：
            messages: 所有消息的列表
            assistant_msg: 包含工具调用的助手消息

        返回：
            如果所有工具调用都有对应的 ToolMessage 则返回 True
        """
        if not hasattr(assistant_msg, "tool_calls") or not assistant_msg.tool_calls:
            return False

        # 获取助手消息中的所有工具调用 ID
        tool_call_ids = {tool_call.get("id") for tool_call in assistant_msg.tool_calls if tool_call.get("id")}

        # 查找助手消息的索引
        try:
            assistant_idx = messages.index(assistant_msg)
        except ValueError:
            return False

        # 获取助手消息之后的所有 ToolMessage
        completed_tool_ids = set()
        for msg in messages[assistant_idx + 1 :]:
            if isinstance(msg, ToolMessage) and msg.tool_call_id:
                completed_tool_ids.add(msg.tool_call_id)

        # 检查所有工具调用是否已完成
        return tool_call_ids.issubset(completed_tool_ids)

    def _create_image_details_message(self, state: ViewImageMiddlewareState) -> list[str | dict]:
        """创建包含所有已查看图片详情的格式化消息。

        参数：
            state: 包含 viewed_images 的当前状态

        返回：
            HumanMessage 的内容块列表（文本和图片）
        """
        viewed_images = state.get("viewed_images", {})
        if not viewed_images:
            # 返回格式正确的文本块，而非纯字符串数组
            return [{"type": "text", "text": "No images have been viewed."}]

        # 构建包含图片信息的消息
        content_blocks: list[str | dict] = [{"type": "text", "text": "Here are the images you've viewed:"}]

        for image_path, image_data in viewed_images.items():
            mime_type = image_data.get("mime_type", "unknown")
            base64_data = image_data.get("base64", "")

            # 添加文本描述
            content_blocks.append({"type": "text", "text": f"\n- **{image_path}** ({mime_type})"})

            # 添加实际的图片数据，使 LLM 可以"看到"它
            if base64_data:
                content_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_data}"},
                    }
                )

        return content_blocks

    def _should_inject_image_message(self, state: ViewImageMiddlewareState) -> bool:
        """判断是否应该注入图片详情消息。

        参数：
            state: 当前状态

        返回：
            如果应该注入消息则返回 True
        """
        messages = state.get("messages", [])
        if not messages:
            return False

        # 获取最后一条助手消息
        last_assistant_msg = self._get_last_assistant_message(messages)
        if not last_assistant_msg:
            return False

        # 检查是否包含 view_image 工具调用
        if not self._has_view_image_tool(last_assistant_msg):
            return False

        # 检查所有工具是否已完成
        if not self._all_tools_completed(messages, last_assistant_msg):
            return False

        # 检查是否已经添加了图片详情消息
        # 在最后一条助手消息之后查找包含图片详情的 human 消息
        assistant_idx = messages.index(last_assistant_msg)
        for msg in messages[assistant_idx + 1 :]:
            if isinstance(msg, HumanMessage):
                content_str = str(msg.content)
                if "Here are the images you've viewed" in content_str or "Here are the details of the images you've viewed" in content_str:
                    # 已添加，不再重复
                    return False

        return True

    def _inject_image_message(self, state: ViewImageMiddlewareState) -> dict | None:
        """注入图片详情消息的内部辅助方法。

        参数：
            state: 当前状态

        返回：
            包含额外 human 消息的状态更新，如果无需更新则返回 None
        """
        if not self._should_inject_image_message(state):
            return None

        # 创建包含文本和图片内容的图片详情消息
        image_content = self._create_image_details_message(state)

        # 创建包含混合内容（文本 + 图片）的新 human 消息
        human_msg = HumanMessage(content=image_content)

        logger.debug("Injecting image details message with images before LLM call")

        # 返回包含新消息的状态更新
        return {"messages": [human_msg]}

    @override
    def before_model(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        """如果 view_image 工具已完成，在 LLM 调用前注入图片详情消息（同步版本）。

        在每次 LLM 调用前运行，检查上一轮是否包含已全部完成的 view_image 工具调用。
        如果是，则注入包含图片详情的 human 消息，使 LLM 可以查看和分析图片。

        参数：
            state: 当前状态
            runtime: 运行时上下文（未使用但接口要求）

        返回：
            包含额外 human 消息的状态更新，如果无需更新则返回 None
        """
        return self._inject_image_message(state)

    @override
    async def abefore_model(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        """如果 view_image 工具已完成，在 LLM 调用前注入图片详情消息（异步版本）。

        在每次 LLM 调用前运行，检查上一轮是否包含已全部完成的 view_image 工具调用。
        如果是，则注入包含图片详情的 human 消息，使 LLM 可以查看和分析图片。

        参数：
            state: 当前状态
            runtime: 运行时上下文（未使用但接口要求）

        返回：
            包含额外 human 消息的状态更新，如果无需更新则返回 None
        """
        return self._inject_image_message(state)
