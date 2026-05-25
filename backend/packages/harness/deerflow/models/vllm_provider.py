"""vLLM 推理引擎适配器 — 保留推理字段（reasoning）的 ChatOpenAI 子类。

模块功能
========
为 vLLM 0.19.0 推理引擎提供 LangChain 兼容的聊天模型适配器。vLLM 通过
OpenAI 兼容的 API 暴露推理模型能力，但 LangChain 的标准 ChatOpenAI 适配器
会丢弃非标准的 reasoning 字段，导致交错思维/工具调用流程中断。

本模块确保 reasoning 字段在以下场景中被正确保留：
- 非流式响应（_create_chat_result）
- 流式增量输出（_convert_chunk_to_generation_chunk）
- 多轮对话请求负载（_get_request_payload）

核心设计
========
1. **推理字段保留**: vLLM 在 assistant 消息中返回非标准的 reasoning 字段，
   后续请求需要原样回传。标准 ChatOpenAI 在序列化时会丢弃该字段，
   本模块在请求负载构建阶段将其重新注入。
2. **chat_template_kwargs 归一化**: DeerFlow 历史上使用 thinking 字段控制
   vLLM 的思维模式，但 vLLM 0.19.0 的 Qwen 推理解析器使用 enable_thinking
   字段。本模块在发送前自动归一化，确保旧配置继续工作。
3. **推理文本提取**: vLLM 的 reasoning 字段可能是字符串、列表或字典，
   本模块提供统一的文本提取逻辑，将各种格式转换为可读的推理文本。
4. **双路消息匹配**: 支持精确位置匹配和基于角色的回退匹配，确保在消息
   数量不一致时也能正确恢复推理字段。

关键特性
========
- assistant 消息 reasoning 字段的多轮保留
- reasoning_content 文本提取（字符串/列表/字典/嵌套结构）
- chat_template_kwargs.thinking → enable_thinking 自动归一化
- 流式和非流式推理内容的统一处理
- 多种消息匹配策略（精确/回退）

使用场景
========
在 config.yaml 中配置 vLLM 推理模型::

    - name: qwen3-32b
      use: deerflow.models.vllm_provider:VllmChatModel
      model: Qwen/Qwen3-32B
      base_url: http://vllm-server:8000/v1
      supports_thinking: true
      when_thinking_enabled:
        extra_body:
          chat_template_kwargs:
            enable_thinking: true

注意事项
========
- 需要 vLLM 0.19.0+ 版本以支持 reasoning 字段
- 旧版配置使用 thinking 字段会被自动归一化为 enable_thinking
- reasoning_content 同时写入 additional_kwargs.reasoning 和
  additional_kwargs.reasoning_content（前者用于回传，后者用于展示）
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

import openai
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessageChunk,
    ChatMessageChunk,
    FunctionMessageChunk,
    HumanMessageChunk,
    SystemMessageChunk,
    ToolMessageChunk,
)
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _create_usage_metadata


def _normalize_vllm_chat_template_kwargs(payload: dict[str, Any]) -> None:
    """将 DeerFlow 的 thinking 字段归一化为 vLLM 的 enable_thinking。

    DeerFlow 历史上在 extra_body.chat_template_kwargs 中使用 thinking 字段
    控制 vLLM 的思维模式，但 vLLM 0.19.0 的 Qwen 推理解析器读取的是
    enable_thinking 字段。此函数在发送前进行归一化，确保旧配置继续工作，
    同时 flash mode 能真正禁用推理。

    Args:
        payload: 待发送的 API 请求负载字典。会被就地修改。
    """
    extra_body = payload.get("extra_body")
    if not isinstance(extra_body, dict):
        return

    chat_template_kwargs = extra_body.get("chat_template_kwargs")
    if not isinstance(chat_template_kwargs, dict):
        return

    # 仅在存在 thinking 字段时进行归一化
    if "thinking" not in chat_template_kwargs:
        return

    # 将 thinking 的值映射到 enable_thinking（如果 enable_thinking 尚未设置）
    normalized_chat_template_kwargs = dict(chat_template_kwargs)
    normalized_chat_template_kwargs.setdefault("enable_thinking", normalized_chat_template_kwargs["thinking"])
    # 移除旧字段，避免 vLLM 同时收到两个冲突的配置
    normalized_chat_template_kwargs.pop("thinking", None)
    extra_body["chat_template_kwargs"] = normalized_chat_template_kwargs


def _reasoning_to_text(reasoning: Any) -> str:
    """从 vLLM 的 reasoning 字段中尽力提取可读的推理文本。

    vLLM 的 reasoning 字段格式不固定，可能是字符串、列表、字典或嵌套结构。
    此函数递归地尝试从各种格式中提取文本内容。

    提取优先级：
    1. 字符串 → 直接返回
    2. 列表 → 递归处理每个元素后拼接
    3. 字典 → 按优先级查找 text/content/reasoning 键
    4. 其他 → 尝试 JSON 序列化，失败则使用 str()

    Args:
        reasoning: vLLM 返回的 reasoning 字段值。

    Returns:
        str: 提取的推理文本。
    """
    if isinstance(reasoning, str):
        return reasoning

    # 递归处理列表中的每个元素
    if isinstance(reasoning, list):
        parts = [_reasoning_to_text(item) for item in reasoning]
        return "".join(part for part in parts if part)

    # 从字典中按优先级提取文本
    if isinstance(reasoning, dict):
        for key in ("text", "content", "reasoning"):
            value = reasoning.get(key)
            if isinstance(value, str):
                return value
            if value is not None:
                text = _reasoning_to_text(value)
                if text:
                    return text
        # 字典中无已知文本键，尝试 JSON 序列化
        try:
            return json.dumps(reasoning, ensure_ascii=False)
        except TypeError:
            return str(reasoning)

    # 兜底：尝试 JSON 序列化，失败则使用 str()
    try:
        return json.dumps(reasoning, ensure_ascii=False)
    except TypeError:
        return str(reasoning)


def _convert_delta_to_message_chunk_with_reasoning(_dict: Mapping[str, Any], default_class: type[BaseMessageChunk]) -> BaseMessageChunk:
    """将流式 delta 转换为 LangChain 消息块，同时保留 reasoning 字段。

    在 LangChain 标准 _convert_delta_to_message_chunk 的基础上，额外处理
    vLLM 返回的 reasoning 字段，将其保存到 additional_kwargs 中。

    reasoning 字段的双重保存：
    - additional_kwargs["reasoning"]: 保留原始值，用于后续请求回传
    - additional_kwargs["reasoning_content"]: 提取的文本，用于前端展示

    Args:
        _dict: vLLM 返回的流式 delta 字典。
        default_class: 默认的消息块类。

    Returns:
        BaseMessageChunk: 包含推理内容的 LangChain 消息块。
    """
    id_ = _dict.get("id")
    role = cast(str, _dict.get("role"))
    content = cast(str, _dict.get("content") or "")
    additional_kwargs: dict[str, Any] = {}

    # 处理 function_call（旧版工具调用格式）
    if _dict.get("function_call"):
        function_call = dict(_dict["function_call"])
        if "name" in function_call and function_call["name"] is None:
            function_call["name"] = ""
        additional_kwargs["function_call"] = function_call

    # 提取并保存 reasoning 字段
    reasoning = _dict.get("reasoning")
    if reasoning is not None:
        additional_kwargs["reasoning"] = reasoning
        reasoning_text = _reasoning_to_text(reasoning)
        if reasoning_text:
            additional_kwargs["reasoning_content"] = reasoning_text

    # 解析工具调用的增量块
    tool_call_chunks = []
    if raw_tool_calls := _dict.get("tool_calls"):
        try:
            tool_call_chunks = [
                tool_call_chunk(
                    name=rtc["function"].get("name"),
                    args=rtc["function"].get("arguments"),
                    id=rtc.get("id"),
                    index=rtc["index"],
                )
                for rtc in raw_tool_calls
            ]
        except KeyError:
            pass

    # 根据角色和默认类创建对应的消息块
    if role == "user" or default_class == HumanMessageChunk:
        return HumanMessageChunk(content=content, id=id_)
    if role == "assistant" or default_class == AIMessageChunk:
        return AIMessageChunk(
            content=content,
            additional_kwargs=additional_kwargs,
            id=id_,
            tool_call_chunks=tool_call_chunks,  # type: ignore[arg-type]
        )
    if role in ("system", "developer") or default_class == SystemMessageChunk:
        role_kwargs = {"__openai_role__": "developer"} if role == "developer" else {}
        return SystemMessageChunk(content=content, id=id_, additional_kwargs=role_kwargs)
    if role == "function" or default_class == FunctionMessageChunk:
        return FunctionMessageChunk(content=content, name=_dict["name"], id=id_)
    if role == "tool" or default_class == ToolMessageChunk:
        return ToolMessageChunk(content=content, tool_call_id=_dict["tool_call_id"], id=id_)
    if role or default_class == ChatMessageChunk:
        return ChatMessageChunk(content=content, role=role, id=id_)  # type: ignore[arg-type]
    return default_class(content=content, id=id_)  # type: ignore[call-arg]


def _restore_reasoning_field(payload_msg: dict[str, Any], orig_msg: AIMessage) -> None:
    """将原始 AIMessage 的 reasoning 字段重新注入到 payload 消息中。

    vLLM 要求在多轮对话中回传 assistant 消息的 reasoning 字段。LangChain
    将 reasoning 存储在 additional_kwargs 中，但序列化时会丢弃该字段。
    此函数在 payload 构建阶段将其恢复。

    优先使用 additional_kwargs["reasoning"]（原始值），若无则使用
    additional_kwargs["reasoning_content"]（文本提取值）。

    Args:
        payload_msg: 序列化后的 payload 消息字典。
        orig_msg: 原始的 AIMessage（包含 reasoning 字段）。
    """
    reasoning = orig_msg.additional_kwargs.get("reasoning")
    if reasoning is None:
        # 回退使用文本提取值
        reasoning = orig_msg.additional_kwargs.get("reasoning_content")
    if reasoning is not None:
        payload_msg["reasoning"] = reasoning


class VllmChatModel(ChatOpenAI):
    """保留 vLLM reasoning 字段的 ChatOpenAI 子类。

    vLLM 0.19.0 通过 OpenAI 兼容 API 暴露推理模型，返回非标准的 reasoning
    字段。本类在 ChatOpenAI 的基础上重写关键方法，确保 reasoning 字段在
    非流式响应、流式增量和多轮对话负载中被正确保留。

    核心能力：
    - 多轮对话中自动回传 assistant 消息的 reasoning 字段
    - 归一化 chat_template_kwargs 中的 thinking → enable_thinking
    - 非流式和流式推理内容的统一提取

    配置示例::

        - name: qwen3-32b
          use: deerflow.models.vllm_provider:VllmChatModel
          model: Qwen/Qwen3-32B
          base_url: http://vllm-server:8000/v1
          supports_thinking: true
          when_thinking_enabled:
            extra_body:
              chat_template_kwargs:
                enable_thinking: true
    """

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        """返回模型类型标识符。"""
        return "vllm-openai-compatible"

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """构建请求负载，恢复 assistant 消息的 reasoning 字段。

        在父类的标准负载基础上，执行两个关键操作：
        1. 归一化 chat_template_kwargs（thinking → enable_thinking）
        2. 将原始 AIMessage 中的 reasoning 字段重新注入到 payload 的
           assistant 消息中，确保 vLLM 能正确处理交错思维/工具调用流程。

        Args:
            input_: LangChain 消息输入。
            stop: 停止词列表（可选）。
            **kwargs: 其他传递给父类的关键字参数。

        Returns:
            dict: 包含 reasoning 字段的 API 请求负载字典。
        """
        # 在转换前捕获原始消息列表
        original_messages = self._convert_input(input_).to_messages()
        # 获取父类的标准负载
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        # 归一化 chat_template_kwargs
        _normalize_vllm_chat_template_kwargs(payload)
        payload_messages = payload.get("messages", [])

        # 恢复 assistant 消息的 reasoning 字段
        if len(payload_messages) == len(original_messages):
            # 精确匹配：消息数量一致
            for payload_msg, orig_msg in zip(payload_messages, original_messages):
                if payload_msg.get("role") == "assistant" and isinstance(orig_msg, AIMessage):
                    _restore_reasoning_field(payload_msg, orig_msg)
        else:
            # 回退匹配：通过角色过滤后进行位置匹配
            ai_messages = [message for message in original_messages if isinstance(message, AIMessage)]
            assistant_payloads = [message for message in payload_messages if message.get("role") == "assistant"]
            for payload_msg, ai_msg in zip(assistant_payloads, ai_messages):
                _restore_reasoning_field(payload_msg, ai_msg)

        return payload

    def _create_chat_result(self, response: dict | openai.BaseModel, generation_info: dict | None = None) -> ChatResult:
        """创建 ChatResult，保留非流式响应中的 reasoning 字段。

        在父类的标准结果基础上，遍历响应中的 choices，将 vLLM 返回的
        reasoning 字段保存到对应 AIMessage 的 additional_kwargs 中。

        reasoning 字段的双重保存：
        - additional_kwargs["reasoning"]: 原始值，用于后续请求回传
        - additional_kwargs["reasoning_content"]: 文本提取值，用于展示

        Args:
            response: vLLM API 返回的响应字典或 openai BaseModel 对象。
            generation_info: 生成信息字典（可选）。

        Returns:
            ChatResult: 包含推理内容的 LangChain 聊天结果。
        """
        result = super()._create_chat_result(response, generation_info=generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump()

        for generation, choice in zip(result.generations, response_dict.get("choices", [])):
            if not isinstance(generation, ChatGeneration):
                continue
            message = generation.message
            if not isinstance(message, AIMessage):
                continue
            # 从响应中提取 reasoning 字段
            reasoning = choice.get("message", {}).get("reasoning")
            if reasoning is None:
                continue
            # 保存原始 reasoning 值（用于后续回传）
            message.additional_kwargs["reasoning"] = reasoning
            # 提取并保存推理文本（用于展示）
            reasoning_text = _reasoning_to_text(reasoning)
            if reasoning_text:
                message.additional_kwargs["reasoning_content"] = reasoning_text

        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        """将流式 chunk 转换为 ChatGenerationChunk，保留 reasoning 字段。

        在父类的标准转换基础上，使用自定义的 delta 转换函数
        _convert_delta_to_message_chunk_with_reasoning，确保流式增量中
        的 reasoning 字段被正确提取和保存。

        Args:
            chunk: vLLM 返回的流式 chunk 字典。
            default_chunk_class: 默认的消息块类。
            base_generation_info: 基础生成信息。

        Returns:
            ChatGenerationChunk | None: 转换后的生成块，无效 chunk 返回 None。
        """
        # 跳过 Responses API 的 content.delta 类型
        if chunk.get("type") == "content.delta":
            return None

        token_usage = chunk.get("usage")
        choices = chunk.get("choices", []) or chunk.get("chunk", {}).get("choices", [])
        usage_metadata = _create_usage_metadata(token_usage, chunk.get("service_tier")) if token_usage else None

        # 无选择项时返回空内容的生成块（携带用量信息）
        if len(choices) == 0:
            generation_chunk = ChatGenerationChunk(message=default_chunk_class(content="", usage_metadata=usage_metadata), generation_info=base_generation_info)
            if self.output_version == "v1":
                generation_chunk.message.content = []
                generation_chunk.message.response_metadata["output_version"] = "v1"
            return generation_chunk

        choice = choices[0]
        if choice["delta"] is None:
            return None

        # 使用自定义的转换函数保留 reasoning 字段
        message_chunk = _convert_delta_to_message_chunk_with_reasoning(choice["delta"], default_chunk_class)
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

        if logprobs := choice.get("logprobs"):
            generation_info["logprobs"] = logprobs

        # 为 AI 消息块附加用量元数据
        if usage_metadata and isinstance(message_chunk, AIMessageChunk):
            message_chunk.usage_metadata = usage_metadata

        message_chunk.response_metadata["model_provider"] = "openai"
        return ChatGenerationChunk(message=message_chunk, generation_info=generation_info or None)
