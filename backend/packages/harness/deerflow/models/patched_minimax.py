"""MiniMax 补丁模型 — 修复推理输出 reasoning_details 字段丢失的问题。

模块功能
========
为 LangChain 的 ChatOpenAI 提供针对 MiniMax 模型的补丁版本，确保 MiniMax
API 返回的推理内容（reasoning_details）被正确提取并映射到 DeerFlow 可识别的
reasoning_content 字段中。

MiniMax 的 OpenAI 兼容 API 在启用 extra_body.reasoning_split=true 后，会在
响应中返回结构化的 reasoning_details 字段，但标准 ChatOpenAI 会忽略该字段，
导致前端无法展示推理过程。

核心设计
========
1. **请求注入**: 在请求负载中自动注入 reasoning_split=true，启用 MiniMax 的
   推理内容分割功能。
2. **多源推理提取**: 支持两种推理内容来源：
   - reasoning_details 字段（结构化的推理摘要列表）
   - 内嵌的 <think >...</think > 标签（模型有时将推理内容混入输出文本）
3. **流式兼容**: 在流式输出中正确处理 reasoning_details 的增量传递，
   保留空格以支持流式拼接。
4. **去重合并**: 合并来自不同来源的推理内容时自动去重，避免重复显示。

关键特性
========
- 自动注入 reasoning_split 参数
- reasoning_details 列表解析
- <think > 标签内联推理提取和清理
- 流式推理内容增量拼接
- 多源推理去重合并

使用场景
========
在 config.yaml 中配置 MiniMax 模型::

    - name: minimax-text-01
      use: deerflow.models.patched_minimax:PatchedChatMiniMax
      model: MiniMax-Text-01
      base_url: https://api.minimax.chat/v1
      supports_thinking: true

注意事项
========
- 需要启用 reasoning_split=true 才能获取结构化的推理内容
- 模型可能同时在 reasoning_details 和 <think > 标签中返回推理内容
- 流式模式下推理内容需要保留空格以支持正确拼接
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import (
    _convert_delta_to_message_chunk,
    _create_usage_metadata,
)

# 匹配 <think >...</think > 标签的正则表达式，用于提取模型内嵌的推理内容
# 有些 MiniMax 模型会将推理内容混入输出文本中，用 <think > 标签包裹
_THINK_TAG_RE = re.compile(r"<think\s*>(.*?)</think\s*>", re.DOTALL)


def _extract_reasoning_text(
    reasoning_details: Any,
    *,
    strip_parts: bool = True,
) -> str | None:
    """从 MiniMax 的 reasoning_details 列表中提取推理文本。

    MiniMax API 返回的 reasoning_details 是一个列表，每个元素是一个映射（字典），
    包含 type 和 text 字段。此函数将所有文本片段拼接为完整的推理内容。

    Args:
        reasoning_details: MiniMax API 返回的推理详情列表。
        strip_parts: 是否对每个片段去除首尾空白。流式模式下应设为 False，
            以保留空格支持增量拼接。

    Returns:
        str | None: 拼接后的推理文本，无内容时返回 None。
    """
    if not isinstance(reasoning_details, list):
        return None

    parts: list[str] = []
    for item in reasoning_details:
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if isinstance(text, str):
            # 流式模式下保留空格，非流式模式下去除空白
            normalized = text.strip() if strip_parts else text
            if normalized.strip():
                parts.append(normalized)

    return "\n\n".join(parts) if parts else None


def _strip_inline_think_tags(content: str) -> tuple[str, str | None]:
    """从输出文本中提取并移除内嵌的 <think > 标签。

    MiniMax 模型有时会将推理内容直接嵌入输出文本中，用 <think >...</think > 标签
    包裹。此函数提取标签内的推理文本，同时从原文中移除标签。

    Args:
        content: 包含可能的 <think > 标签的输出文本。

    Returns:
        tuple[str, str | None]: 元组包含两个元素：
            - str: 移除 <think > 标签后的干净文本
            - str | None: 提取的推理内容，无标签时返回 None
    """
    reasoning_parts: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        """正则替换回调：提取推理内容并返回空字符串替换标签。"""
        reasoning = match.group(1).strip()
        if reasoning:
            reasoning_parts.append(reasoning)
        return ""

    cleaned = _THINK_TAG_RE.sub(_replace, content).strip()
    reasoning = "\n\n".join(reasoning_parts) if reasoning_parts else None
    return cleaned, reasoning


def _merge_reasoning(*values: str | None) -> str | None:
    """合并多个推理内容来源，自动去重。

    MiniMax 可能同时通过 reasoning_details 和 <think > 标签返回推理内容，
    此函数将它们合并为统一的推理文本，同时避免重复内容。

    Args:
        *values: 多个推理文本片段（可能为 None）。

    Returns:
        str | None: 合并后的推理文本，全部为空时返回 None。
    """
    merged: list[str] = []
    for value in values:
        if not value:
            continue
        normalized = value.strip()
        # 跳过空内容和重复内容
        if normalized and normalized not in merged:
            merged.append(normalized)
    return "\n\n".join(merged) if merged else None


def _with_reasoning_content(
    message: AIMessage | AIMessageChunk,
    reasoning: str | None,
    *,
    preserve_whitespace: bool = False,
):
    """为消息对象添加 reasoning_content 到 additional_kwargs。

    Args:
        message: 待修改的 AIMessage 或 AIMessageChunk。
        reasoning: 要添加的推理内容。
        preserve_whitespace: 是否保留空格（流式模式下为 True）。
            流式模式下直接拼接，非流式模式下先去重合并。

    Returns:
        AIMessage | AIMessageChunk: 添加了 reasoning_content 的消息副本。
    """
    if not reasoning:
        return message

    additional_kwargs = dict(message.additional_kwargs)
    if preserve_whitespace:
        # 流式模式：直接拼接推理内容，保留空格
        existing = additional_kwargs.get("reasoning_content")
        additional_kwargs["reasoning_content"] = f"{existing}{reasoning}" if isinstance(existing, str) else reasoning
    else:
        # 非流式模式：合并去重
        additional_kwargs["reasoning_content"] = _merge_reasoning(
            additional_kwargs.get("reasoning_content"),
            reasoning,
        )
    return message.model_copy(update={"additional_kwargs": additional_kwargs})


class PatchedChatMiniMax(ChatOpenAI):
    """MiniMax 推理输出补丁版本的 ChatOpenAI 适配器。

    本类在标准 ChatOpenAI 的基础上增加了以下能力：
    - 自动注入 reasoning_split=true 参数
    - 从 reasoning_details 字段提取推理内容
    - 从内嵌 <think > 标签提取推理内容
    - 多源推理内容去重合并
    - 流式推理内容增量拼接

    配置示例::

        - name: minimax-text-01
          use: deerflow.models.patched_minimax:PatchedChatMiniMax
          model: MiniMax-Text-01
          base_url: https://api.minimax.chat/v1
    """

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """构建请求负载，自动注入 reasoning_split 参数。

        在父类生成的标准负载基础上，确保 extra_body 中包含 reasoning_split=true，
        启用 MiniMax 的推理内容分割功能。

        Args:
            input_: LangChain 消息输入。
            stop: 停止词列表（可选）。
            **kwargs: 其他传递给父类的关键字参数。

        Returns:
            dict: 包含 reasoning_split 的 API 请求负载字典。
        """
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        # 注入 reasoning_split=true 以启用推理内容分割
        extra_body = payload.get("extra_body")
        if isinstance(extra_body, dict):
            payload["extra_body"] = {
                **extra_body,
                "reasoning_split": True,
            }
        else:
            payload["extra_body"] = {"reasoning_split": True}
        return payload

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        """将流式 chunk 转换为 ChatGenerationChunk，保留推理内容。

        在父类的标准转换基础上，额外提取 delta 中的 reasoning_details 字段，
        并将其映射到 AIMessageChunk 的 additional_kwargs.reasoning_content 中。

        流式模式下推理内容需要保留空格（preserve_whitespace=True），
        以支持多个 chunk 之间的正确拼接。

        Args:
            chunk: OpenAI 兼容 API 返回的流式 chunk 字典。
            default_chunk_class: 默认的消息块类。
            base_generation_info: 基础生成信息。

        Returns:
            ChatGenerationChunk | None: 转换后的生成块，无效 chunk 返回 None。
        """
        # 跳过 Responses API 的 content.delta 类型（非 Chat Completions 格式）
        if chunk.get("type") == "content.delta":
            return None

        token_usage = chunk.get("usage")
        choices = chunk.get("choices", []) or chunk.get("chunk", {}).get("choices", [])
        usage_metadata = _create_usage_metadata(token_usage, chunk.get("service_tier")) if token_usage else None

        # 无选择项时返回空内容的生成块（携带用量信息）
        if len(choices) == 0:
            generation_chunk = ChatGenerationChunk(
                message=default_chunk_class(content="", usage_metadata=usage_metadata),
                generation_info=base_generation_info,
            )
            if self.output_version == "v1":
                generation_chunk.message.content = []
                generation_chunk.message.response_metadata["output_version"] = "v1"
            return generation_chunk

        choice = choices[0]
        delta = choice.get("delta")
        if delta is None:
            return None

        # 使用父类的标准转换，然后添加推理内容
        message_chunk = _convert_delta_to_message_chunk(delta, default_chunk_class)
        generation_info = {**base_generation_info} if base_generation_info else {}

        # 收集生成元数据
        if finish_reason := choice.get("finish_reason"):
            generation_info["finish_reason"] = finish_reason
            if model_name := chunk.get("model"):
                generation_info["model_name"] = model_name
            if system_fingerprint := chunk.get("system_fingerprint"):
                generation_info["system_fingerprint"] = system_fingerprint
            if service_tier := chunk.get("service_tier"):
                generation_info["service_tier"] = service_tier

        logprobs = choice.get("logprobs")
        if logprobs:
            generation_info["logprobs"] = logprobs

        # 提取推理内容，流式模式下保留空格以支持拼接
        reasoning = _extract_reasoning_text(
            delta.get("reasoning_details"),
            strip_parts=False,
        )
        if isinstance(message_chunk, AIMessageChunk):
            if usage_metadata:
                message_chunk.usage_metadata = usage_metadata
            if reasoning:
                message_chunk = _with_reasoning_content(
                    message_chunk,
                    reasoning,
                    preserve_whitespace=True,  # 流式模式保留空格
                )

        message_chunk.response_metadata["model_provider"] = "openai"
        return ChatGenerationChunk(
            message=message_chunk,
            generation_info=generation_info or None,
        )

    def _create_chat_result(
        self,
        response: dict | Any,
        generation_info: dict | None = None,
    ) -> ChatResult:
        """创建 ChatResult，合并多源推理内容。

        在父类的标准结果基础上，处理两种推理内容来源：
        1. reasoning_details 字段（结构化推理摘要）
        2. <think > 标签（内嵌在输出文本中的推理内容）

        两种来源的内容会被合并去重，同时清理输出文本中的 <think > 标签。

        Args:
            response: API 响应字典或模型对象。
            generation_info: 生成信息字典（可选）。

        Returns:
            ChatResult: 包含推理内容的 LangChain 聊天结果。
        """
        result = super()._create_chat_result(response, generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump()
        choices = response_dict.get("choices", [])

        generations: list[ChatGeneration] = []
        for index, generation in enumerate(result.generations):
            choice = choices[index] if index < len(choices) else {}
            message = generation.message
            if isinstance(message, AIMessage):
                content = message.content if isinstance(message.content, str) else None
                cleaned_content = content
                inline_reasoning = None
                if isinstance(content, str):
                    # 提取并清理内嵌的 <think > 标签推理内容
                    cleaned_content, inline_reasoning = _strip_inline_think_tags(content)

                # 提取 reasoning_details 字段中的结构化推理内容
                choice_message = choice.get("message", {}) if isinstance(choice, Mapping) else {}
                split_reasoning = _extract_reasoning_text(choice_message.get("reasoning_details"))
                # 合并两种来源的推理内容，自动去重
                merged_reasoning = _merge_reasoning(split_reasoning, inline_reasoning)

                updated_message = message
                # 更新清理后的文本内容（移除了 <think > 标签）
                if cleaned_content is not None and cleaned_content != message.content:
                    updated_message = updated_message.model_copy(update={"content": cleaned_content})
                # 注入合并后的推理内容
                if merged_reasoning:
                    updated_message = _with_reasoning_content(updated_message, merged_reasoning)

                generation = ChatGeneration(
                    message=updated_message,
                    generation_info=generation.generation_info,
                )

            generations.append(generation)

        return ChatResult(generations=generations, llm_output=result.llm_output)
