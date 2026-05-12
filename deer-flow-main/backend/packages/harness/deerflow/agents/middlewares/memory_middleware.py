"""记忆机制中间件。

在智能体执行后将对话排队进行记忆更新，
仅包含用户输入和最终助手响应（忽略工具调用），
使用防抖机制批量处理多个更新。
"""

import logging
import re
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config
from langgraph.runtime import Runtime

from deerflow.agents.memory.queue import get_memory_queue
from deerflow.config.memory_config import get_memory_config

logger = logging.getLogger(__name__)


class MemoryMiddlewareState(AgentState):
    """与 `ThreadState` 模式兼容。"""

    pass


def _filter_messages_for_memory(messages: list[Any]) -> list[Any]:
    """过滤消息，仅保留用户输入和最终助手响应。

    过滤掉：
    - 工具消息（中间工具调用结果）
    - 带有 tool_calls 的 AI 消息（中间步骤，非最终响应）
    - UploadsMiddleware 注入到 human 消息中的 <uploaded_files> 块
      （文件路径是会话范围的，不应持久化到长期记忆中）。
      用户的实际问题会被保留；只有内容完全是上传块（去除后无剩余）的轮次
      才会连同其配对的助手响应一起被丢弃。

    仅保留：
    - Human 消息（已移除临时上传块）
    - 不带 tool_calls 的 AI 消息（最终助手响应），除非配对的 human 轮次
      仅包含上传且没有真实用户文本。

    参数：
        messages: 所有对话消息的列表。

    返回：
        仅包含用户输入和最终助手响应的过滤列表。
    """
    _UPLOAD_BLOCK_RE = re.compile(r"<uploaded_files>[\s\S]*?</uploaded_files>\n*", re.IGNORECASE)

    filtered = []
    skip_next_ai = False
    for msg in messages:
        msg_type = getattr(msg, "type", None)

        if msg_type == "human":
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            content_str = str(content)
            if "<uploaded_files>" in content_str:
                # Strip the ephemeral upload block; keep the user's real question.
                stripped = _UPLOAD_BLOCK_RE.sub("", content_str).strip()
                if not stripped:
                    # Nothing left — the entire turn was upload bookkeeping;
                    # skip it and the paired assistant response.
                    skip_next_ai = True
                    continue
                # Rebuild the message with cleaned content so the user's question
                # is still available for memory summarisation.
                from copy import copy

                clean_msg = copy(msg)
                clean_msg.content = stripped
                filtered.append(clean_msg)
                skip_next_ai = False
            else:
                filtered.append(msg)
                skip_next_ai = False
        elif msg_type == "ai":
            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                if skip_next_ai:
                    skip_next_ai = False
                    continue
                filtered.append(msg)
        # Skip tool messages and AI messages with tool_calls

    return filtered


class MemoryMiddleware(AgentMiddleware[MemoryMiddlewareState]):
    """在智能体执行后将对话排队进行记忆更新的中间件。

    此中间件：
    1. 在每次智能体执行后，将对话排队进行记忆更新
    2. 仅包含用户输入和最终助手响应（忽略工具调用）
    3. 队列使用防抖机制批量处理多个更新
    4. 记忆通过 LLM 摘要异步更新
    """

    state_schema = MemoryMiddlewareState

    def __init__(self, agent_name: str | None = None):
        """初始化 MemoryMiddleware。

        参数：
            agent_name: 如果提供，则按智能体存储记忆。如果为 None，则使用全局记忆。
        """
        super().__init__()
        self._agent_name = agent_name

    @override
    def after_agent(self, state: MemoryMiddlewareState, runtime: Runtime) -> dict | None:
        """在智能体完成后将对话排队进行记忆更新。

        参数：
            state: 当前智能体状态。
            runtime: 运行时上下文。

        返回：
            None（此中间件不需要状态变更）。
        """
        config = get_memory_config()
        if not config.enabled:
            return None

        # Get thread ID from runtime context first, then fall back to LangGraph's configurable metadata
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if thread_id is None:
            config_data = get_config()
            thread_id = config_data.get("configurable", {}).get("thread_id")
        if not thread_id:
            logger.debug("No thread_id in context, skipping memory update")
            return None

        # Get messages from state
        messages = state.get("messages", [])
        if not messages:
            logger.debug("No messages in state, skipping memory update")
            return None

        # Filter to only keep user inputs and final assistant responses
        filtered_messages = _filter_messages_for_memory(messages)

        # Only queue if there's meaningful conversation
        # At minimum need one user message and one assistant response
        user_messages = [m for m in filtered_messages if getattr(m, "type", None) == "human"]
        assistant_messages = [m for m in filtered_messages if getattr(m, "type", None) == "ai"]

        if not user_messages or not assistant_messages:
            return None

        # Queue the filtered conversation for memory update
        queue = get_memory_queue()
        queue.add(thread_id=thread_id, messages=filtered_messages, agent_name=self._agent_name)

        return None
