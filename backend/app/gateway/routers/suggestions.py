"""对话后续建议（Follow-up Suggestions）生成路由。

本模块利用 AI 模型根据最近的对话上下文，自动生成用户可能感兴趣的
后续问题建议。这些建议以列表形式展示在前端界面中，帮助用户
快速继续对话。

工作原理：
1. 接收最近的对话消息列表
2. 构建 system prompt，指导 AI 生成简短、相关的后续问题
3. 调用配置的聊天模型生成建议
4. 解析模型返回的 JSON 数组（支持 Markdown 代码块包裹）
5. 清理并截断到请求的数量

生成规则：
- 建议必须与上下文相关
- 使用与用户相同的语言
- 每个建议保持简洁（英文 <= 20 词，中文 <= 40 字）
- 输出为纯 JSON 字符串数组，不含编号或额外格式

错误处理：
- 生成失败时返回空列表，不中断前端流程

路由前缀：/api
标签：suggestions
"""

import json
import logging

from fastapi import APIRouter, Depends, Request
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.gateway.authz import require_permission
from app.gateway.deps import get_config
from deerflow.config.app_config import AppConfig
from deerflow.models import create_chat_model

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["suggestions"])


class SuggestionMessage(BaseModel):
    """对话消息模型，用于传入对话上下文。

    Attributes:
        role: 消息角色（user 或 assistant）。
        content: 消息纯文本内容。
    """

    role: str = Field(..., description="Message role: user|assistant")
    content: str = Field(..., description="Message content as plain text")


class SuggestionsRequest(BaseModel):
    """建议生成请求模型。

    Attributes:
        messages: 最近的对话消息列表。
        n: 要生成的建议数量（1-5，默认 3）。
        model_name: 可选的模型名称覆盖。
    """

    messages: list[SuggestionMessage] = Field(..., description="Recent conversation messages")
    n: int = Field(default=3, ge=1, le=5, description="Number of suggestions to generate")
    model_name: str | None = Field(default=None, description="Optional model override")


class SuggestionsResponse(BaseModel):
    """建议生成响应模型。

    Attributes:
        suggestions: 生成的后续问题列表。
    """

    suggestions: list[str] = Field(default_factory=list, description="Suggested follow-up questions")


def _strip_markdown_code_fence(text: str) -> str:
    """去除模型输出中可能包裹的 Markdown 代码块标记。

    AI 模型有时会将 JSON 输出包裹在 ```json...``` 代码块中，
    需要提取其中的实际内容。

    Args:
        text: 原始模型输出文本。

    Returns:
        去除代码块标记后的文本。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _parse_json_string_list(text: str) -> list[str] | None:
    """从模型输出文本中提取 JSON 字符串数组。

    容错策略：
    1. 先尝试去除 Markdown 代码块
    2. 定位第一个 [ 和最后一个 ] 提取 JSON 子串
    3. 解析为数组并过滤非字符串/空字符串元素

    Args:
        text: 模型输出文本。

    Returns:
        字符串列表，或解析失败时返回 None。
    """
    candidate = _strip_markdown_code_fence(text)
    start = candidate.find("[")
    end = candidate.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = candidate[start : end + 1]
    try:
        data = json.loads(candidate)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    # 过滤非字符串和空字符串条目
    out: list[str] = []
    for item in data:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s:
            continue
        out.append(s)
    return out


def _extract_response_text(content: object) -> str:
    """从模型响应内容中提取纯文本。

    处理模型响应的多种内容格式：字符串、内容块列表、空值等。

    Args:
        content: 模型响应的原始内容。

    Returns:
        提取并拼接的纯文本字符串。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts) if parts else ""
    if content is None:
        return ""
    return str(content)


def _format_conversation(messages: list[SuggestionMessage]) -> str:
    """将对话消息列表格式化为供 AI 模型使用的文本上下文。

    Args:
        messages: 对话消息列表。

    Returns:
        格式化后的对话文本。
    """
    parts: list[str] = []
    for m in messages:
        role = m.role.strip().lower()
        if role in ("user", "human"):
            parts.append(f"User: {m.content.strip()}")
        elif role in ("assistant", "ai"):
            parts.append(f"Assistant: {m.content.strip()}")
        else:
            parts.append(f"{m.role}: {m.content.strip()}")
    return "\n".join(parts).strip()


@router.post(
    "/threads/{thread_id}/suggestions",
    response_model=SuggestionsResponse,
    summary="Generate Follow-up Questions",
    description="Generate short follow-up questions a user might ask next, based on recent conversation context.",
)
@require_permission("threads", "read", owner_check=True)
async def generate_suggestions(
    thread_id: str,
    body: SuggestionsRequest,
    request: Request,
    config: AppConfig = Depends(get_config),
) -> SuggestionsResponse:
    """基于对话上下文生成后续问题建议。

    调用 AI 模型根据最近的对话内容生成用户可能想问的后续问题。
    生成失败时静默返回空列表，不中断前端交互。

    Args:
        thread_id: 线程 ID。
        body: 建议生成请求体。
        request: FastAPI 请求对象。
        config: 应用配置对象。

    Returns:
        SuggestionsResponse，包含生成的建议列表。
    """
    # 无消息时直接返回空列表
    if not body.messages:
        return SuggestionsResponse(suggestions=[])

    n = body.n
    conversation = _format_conversation(body.messages)
    if not conversation:
        return SuggestionsResponse(suggestions=[])

    # 构建 system prompt，指导模型生成简短、相关的后续问题
    system_instruction = (
        "You are generating follow-up questions to help the user continue the conversation.\n"
        f"Based on the conversation below, produce EXACTLY {n} short questions the user might ask next.\n"
        "Requirements:\n"
        "- Questions must be relevant to the preceding conversation.\n"
        "- Questions must be written in the same language as the user.\n"
        "- Keep each question concise (ideally <= 20 words / <= 40 Chinese characters).\n"
        "- Do NOT include numbering, markdown, or any extra text.\n"
        "- Output MUST be a JSON array of strings only.\n"
    )
    user_content = f"Conversation Context:\n{conversation}\n\nGenerate {n} follow-up questions"

    try:
        # 使用配置的模型生成建议（禁用思考模式以加快响应速度）
        model = create_chat_model(name=body.model_name, thinking_enabled=False, app_config=config)
        response = await model.ainvoke([SystemMessage(content=system_instruction), HumanMessage(content=user_content)], config={"run_name": "suggest_agent"})
        raw = _extract_response_text(response.content)
        suggestions = _parse_json_string_list(raw) or []
        # 清理换行并截断到请求数量
        cleaned = [s.replace("\n", " ").strip() for s in suggestions if s.strip()]
        cleaned = cleaned[:n]
        return SuggestionsResponse(suggestions=cleaned)
    except Exception as exc:
        # 生成失败时静默降级，返回空列表不中断前端
        logger.exception("Failed to generate suggestions: thread_id=%s err=%s", thread_id, exc)
        return SuggestionsResponse(suggestions=[])
