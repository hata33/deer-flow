"""自动线程标题生成中间件。"""

import logging
from typing import NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

from deerflow.config.title_config import get_title_config
from deerflow.models import create_chat_model

logger = logging.getLogger(__name__)


class TitleMiddlewareState(AgentState):
    """与 `ThreadState` 模式兼容。"""

    title: NotRequired[str | None]


class TitleMiddleware(AgentMiddleware[TitleMiddlewareState]):
    """在第一条用户消息后自动生成线程标题。"""

    state_schema = TitleMiddlewareState

    def _normalize_content(self, content: object) -> str:
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            parts = [self._normalize_content(item) for item in content]
            return "\n".join(part for part in parts if part)

        if isinstance(content, dict):
            text_value = content.get("text")
            if isinstance(text_value, str):
                return text_value

            nested_content = content.get("content")
            if nested_content is not None:
                return self._normalize_content(nested_content)

        return ""

    def _should_generate_title(self, state: TitleMiddlewareState) -> bool:
        """检查是否应该为此线程生成标题。"""
        config = get_title_config()
        if not config.enabled:
            return False

        # Check if thread already has a title in state
        if state.get("title"):
            return False

        # Check if this is the first turn (has at least one user message and one assistant response)
        messages = state.get("messages", [])
        if len(messages) < 2:
            return False

        # Count user and assistant messages
        user_messages = [m for m in messages if m.type == "human"]
        assistant_messages = [m for m in messages if m.type == "ai"]

        # Generate title after first complete exchange
        return len(user_messages) == 1 and len(assistant_messages) >= 1

    def _build_title_prompt(self, state: TitleMiddlewareState) -> tuple[str, str]:
        """提取用户/助手消息并构建标题提示词。

        返回 (prompt_string, user_msg)，调用者可将 user_msg 作为回退使用。
        """
        config = get_title_config()
        messages = state.get("messages", [])

        user_msg_content = next((m.content for m in messages if m.type == "human"), "")
        assistant_msg_content = next((m.content for m in messages if m.type == "ai"), "")

        user_msg = self._normalize_content(user_msg_content)
        assistant_msg = self._normalize_content(assistant_msg_content)

        prompt = config.prompt_template.format(
            max_words=config.max_words,
            user_msg=user_msg[:500],
            assistant_msg=assistant_msg[:500],
        )
        return prompt, user_msg

    def _parse_title(self, content: object) -> str:
        """将模型输出标准化为干净的标题字符串。"""
        config = get_title_config()
        title_content = self._normalize_content(content)
        title = title_content.strip().strip('"').strip("'")
        return title[: config.max_chars] if len(title) > config.max_chars else title

    def _fallback_title(self, user_msg: str) -> str:
        config = get_title_config()
        fallback_chars = min(config.max_chars, 50)
        if len(user_msg) > fallback_chars:
            return user_msg[:fallback_chars].rstrip() + "..."
        return user_msg if user_msg else "New Conversation"

    def _generate_title_result(self, state: TitleMiddlewareState) -> dict | None:
        """同步生成标题。返回状态更新或 None。"""
        if not self._should_generate_title(state):
            return None

        prompt, user_msg = self._build_title_prompt(state)
        config = get_title_config()
        model = create_chat_model(name=config.model_name, thinking_enabled=False)

        try:
            response = model.invoke(prompt)
            title = self._parse_title(response.content)
            if not title:
                title = self._fallback_title(user_msg)
        except Exception:
            logger.exception("Failed to generate title (sync)")
            title = self._fallback_title(user_msg)

        return {"title": title}

    async def _agenerate_title_result(self, state: TitleMiddlewareState) -> dict | None:
        """异步生成标题。返回状态更新或 None。"""
        if not self._should_generate_title(state):
            return None

        prompt, user_msg = self._build_title_prompt(state)
        config = get_title_config()
        model = create_chat_model(name=config.model_name, thinking_enabled=False)

        try:
            response = await model.ainvoke(prompt)
            title = self._parse_title(response.content)
            if not title:
                title = self._fallback_title(user_msg)
        except Exception:
            logger.exception("Failed to generate title (async)")
            title = self._fallback_title(user_msg)

        return {"title": title}

    @override
    def after_model(self, state: TitleMiddlewareState, runtime: Runtime) -> dict | None:
        return self._generate_title_result(state)

    @override
    async def aafter_model(self, state: TitleMiddlewareState, runtime: Runtime) -> dict | None:
        return await self._agenerate_title_result(state)
