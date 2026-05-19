"""
LangChain 消息对象转换为 OpenAI Chat Completions 格式的纯函数工具模块。

用于将 LangChain 消息类型转换为 OpenAI 兼容的字典格式。
目前未集成到 RunJournal 中（RunJournal 直接使用 message.model_dump()），
但可供需要 OpenAI 线格式的消费者使用。
"""

from __future__ import annotations

import json
from typing import Any

# LangChain 消息类型到 OpenAI 角色的映射表
_ROLE_MAP = {
    "human": "user",      # 用户消息
    "ai": "assistant",    # AI 助手消息
    "system": "system",   # 系统消息
    "tool": "tool",       # 工具调用消息
}


def langchain_to_openai_message(message: Any) -> dict:
    """将单个 LangChain BaseMessage 转换为 OpenAI 消息字典。

    支持的转换类型:
    - HumanMessage → {"role": "user", "content": "..."}
    - AIMessage (仅文本) → {"role": "assistant", "content": "..."}
    - AIMessage (包含 tool_calls) → {"role": "assistant", "content": null, "tool_calls": [...]}
    - AIMessage (文本 + tool_calls) → 同时包含 content 和 tool_calls
    - AIMessage (列表内容 / 多模态) → content 保留为列表格式
    - SystemMessage → {"role": "system", "content": "..."}
    - ToolMessage → {"role": "tool", "tool_call_id": "...", "content": "..."}

    Args:
        message: LangChain BaseMessage 对象

    Returns:
        OpenAI 格式的消息字典
    """
    # 获取消息类型并映射到 OpenAI 角色
    msg_type = getattr(message, "type", "")
    role = _ROLE_MAP.get(msg_type, msg_type)
    content = getattr(message, "content", "")

    # 处理工具消息
    if role == "tool":
        return {
            "role": "tool",
            "tool_call_id": getattr(message, "tool_call_id", ""),
            "content": content,
        }

    # 处理 AI 助手消息
    if role == "assistant":
        tool_calls = getattr(message, "tool_calls", None) or []
        result: dict = {"role": "assistant"}

        if tool_calls:
            # 转换工具调用为 OpenAI 格式
            openai_tool_calls = []
            for tc in tool_calls:
                args = tc.get("args", {})
                openai_tool_calls.append(
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            # 将参数转换为 JSON 字符串（如果还不是字符串）
                            "arguments": json.dumps(args) if not isinstance(args, str) else args,
                        },
                    }
                )
            # 如果没有文本内容，按 OpenAI 规范将 content 设为 null
            result["content"] = content if (isinstance(content, list) and content) or (isinstance(content, str) and content) else None
            result["tool_calls"] = openai_tool_calls
        else:
            result["content"] = content

        return result

    # 处理用户/系统/未知类型的消息
    return {"role": role, "content": content}


def _infer_finish_reason(message: Any) -> str:
    """从 AIMessage 推断 OpenAI finish_reason。

    推断逻辑:
    1. 如果存在 tool_calls，返回 "tool_calls"
    2. 否则从 response_metadata.finish_reason 获取
    3. 默认返回 "stop"

    Args:
        message: LangChain AIMessage 对象

    Returns:
        finish_reason 字符串
    """
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        return "tool_calls"
    resp_meta = getattr(message, "response_metadata", None) or {}
    if isinstance(resp_meta, dict):
        finish = resp_meta.get("finish_reason")
        if finish:
            return finish
    return "stop"


def langchain_to_openai_completion(message: Any) -> dict:
    """将 AIMessage 及其元数据转换为 OpenAI completion 响应字典。

    Returns:
        {
            "id": message.id,
            "model": message.response_metadata.get("model_name"),
            "choices": [{"index": 0, "message": <openai_message>, "finish_reason": <inferred>}],
            "usage": {"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...} or None,
        }

    Args:
        message: LangChain AIMessage 对象

    Returns:
        OpenAI completion 格式的响应字典
    """
    resp_meta = getattr(message, "response_metadata", None) or {}
    model_name = resp_meta.get("model_name") if isinstance(resp_meta, dict) else None

    # 转换消息为 OpenAI 格式
    openai_msg = langchain_to_openai_message(message)
    finish_reason = _infer_finish_reason(message)

    # 提取使用量信息
    usage_metadata = getattr(message, "usage_metadata", None)
    if usage_metadata is not None:
        input_tokens = usage_metadata.get("input_tokens", 0) or 0
        output_tokens = usage_metadata.get("output_tokens", 0) or 0
        usage: dict | None = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
    else:
        usage = None

    return {
        "id": getattr(message, "id", None),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": openai_msg,
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }


def langchain_messages_to_openai(messages: list) -> list[dict]:
    """将 LangChain BaseMessage 列表转换为 OpenAI 消息字典列表。

    Args:
        messages: LangChain BaseMessage 对象列表

    Returns:
        OpenAI 格式的消息字典列表
    """
    return [langchain_to_openai_message(m) for m in messages]
