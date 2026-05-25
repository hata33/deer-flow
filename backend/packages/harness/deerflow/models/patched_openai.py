"""OpenAI 补丁模型 — 修复 Gemini 思维签名（thought_signature）丢失的问题。

模块功能
========
为 LangChain 的 ChatOpenAI 提供补丁版本，解决通过 OpenAI 兼容网关使用
Gemini 思维模型时，工具调用对象上的 thought_signature 字段在多轮对话中被
静默丢弃的问题。

当使用 Gemini 并启用思维模式（thinking）时，OpenAI 兼容网关会在每个工具调用
对象上附加一个 thought_signature 字段。后续请求必须原样回传该签名，否则
API 将返回 HTTP 400 INVALID_ARGUMENT 错误::

    Unable to submit request because function call `<tool>` in the N. content
    block is missing a `thought_signature`.

核心设计
========
1. **签名保存**: LangChain 将原始的工具调用字典（包含 thought_signature）
   保存在 AIMessage.additional_kwargs["tool_calls"] 中。
2. **签名恢复**: 在构建请求负载时，从 additional_kwargs 中提取签名，
   重新注入到序列化后的 payload 工具调用对象中。
3. **双格式兼容**: 支持 snake_case（thought_signature）和 camelCase
   （thoughtSignature）两种签名格式。
4. **灵活匹配**: 优先按 ID 匹配工具调用，ID 不可用时回退到位置匹配。

问题背景
========
LangChain 的 ChatOpenAI 在序列化工具调用时，仅保留标准字段（id、type、function），
丢弃了 thought_signature 等非标准字段。这对于 OpenAI 原生 API 不影响，
但 Gemini 的 OpenAI 兼容网关严格要求该签名的回传。

使用场景
========
通过 OpenAI 兼容网关使用 Gemini 思维模型::

    - name: gemini-2.5-pro-thinking
      use: deerflow.models.patched_openai:PatchedChatOpenAI
      model: google/gemini-2.5-pro-preview
      base_url: https://<your-openai-compat-gateway>/v1
      supports_thinking: true
      supports_vision: true
      when_thinking_enabled:
        extra_body:
          thinking:
            type: enabled

注意事项
========
- 仅在使用 Gemini 思维模型时需要此补丁
- 对标准 OpenAI 模型无影响（不存在 thought_signature 字段）
- 对非思维模式的 Gemini 模型无影响（工具调用不携带签名）
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI


class PatchedChatOpenAI(ChatOpenAI):
    """修复 thought_signature 保留问题的 ChatOpenAI 补丁版本。

    当通过 OpenAI 兼容网关使用 Gemini 思维模型时，API 要求工具调用对象
    携带 thought_signature 字段。本类重写 _get_request_payload 方法，
    从 AIMessage.additional_kwargs["tool_calls"] 中恢复签名到序列化后的
    请求负载中。

    签名恢复流程：
    1. 在消息转换前捕获原始 LangChain 消息列表
    2. 调用父类方法获取标准 payload（此时签名已被丢弃）
    3. 匹配 payload 中的 assistant 消息与原始 AIMessage
    4. 从原始消息的 additional_kwargs 中提取签名并注入 payload

    配置示例::

        - name: gemini-2.5-pro-thinking
          use: deerflow.models.patched_openai:PatchedChatOpenAI
          model: google/gemini-2.5-pro-preview
          api_key: $GEMINI_API_KEY
          base_url: https://<your-openai-compat-gateway>/v1
          max_tokens: 16384
          supports_thinking: true
          supports_vision: true
          when_thinking_enabled:
            extra_body:
              thinking:
                type: enabled
    """

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """构建包含 thought_signature 的请求负载。

        在父类生成的标准负载基础上，遍历所有 assistant 消息的工具调用，
        从原始 AIMessage 的 additional_kwargs["tool_calls"] 中提取
        thought_signature，重新注入到 payload 对应的工具调用对象中。

        匹配策略：
        1. 精确匹配：payload 和原始消息数量一致时，按位置一一对应
        2. 回退匹配：数量不一致时，分别收集 assistant 角色的消息进行匹配

        Args:
            input_: LangChain 消息输入（消息列表或提示模板）。
            stop: 停止词列表（可选）。
            **kwargs: 其他传递给父类的关键字参数。

        Returns:
            dict: 包含 thought_signature 的 API 请求负载字典。
        """
        # 在转换前捕获原始消息，以便访问被序列化器丢弃的字段
        original_messages = self._convert_input(input_).to_messages()

        # 调用父类获取标准 payload（此时 thought_signature 已被丢弃）
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        payload_messages = payload.get("messages", [])

        if len(payload_messages) == len(original_messages):
            # 精确匹配：消息数量一致，按位置一一对应
            for payload_msg, orig_msg in zip(payload_messages, original_messages):
                if payload_msg.get("role") == "assistant" and isinstance(orig_msg, AIMessage):
                    _restore_tool_call_signatures(payload_msg, orig_msg)
        else:
            # 回退匹配：通过角色过滤后进行位置匹配
            ai_messages = [m for m in original_messages if isinstance(m, AIMessage)]
            assistant_payloads = [(i, m) for i, m in enumerate(payload_messages) if m.get("role") == "assistant"]
            for (_, payload_msg), ai_msg in zip(assistant_payloads, ai_messages):
                _restore_tool_call_signatures(payload_msg, ai_msg)

        return payload


def _restore_tool_call_signatures(payload_msg: dict, orig_msg: AIMessage) -> None:
    """从原始消息中恢复工具调用的 thought_signature 到 payload。

    Gemini OpenAI 兼容网关返回的工具调用可能携带 thought_signature 字段，
    LangChain 将原始工具调用字典存储在 additional_kwargs["tool_calls"] 中，
    但在序列化时只保留标准字段（id、type、function），丢弃了签名。

    此函数通过 ID 或位置匹配原始工具调用和 payload 工具调用，
    将签名字段复制回 payload 中。

    匹配策略：
    1. 优先按 ID 精确匹配（高效且准确）
    2. ID 匹配失败时回退到位置匹配（兜底方案）

    Args:
        payload_msg: 序列化后的 payload 消息字典（工具调用中缺少签名）。
        orig_msg: 原始的 AIMessage（additional_kwargs 中保存了完整工具调用）。
    """
    raw_tool_calls: list[dict] = orig_msg.additional_kwargs.get("tool_calls") or []
    payload_tool_calls: list[dict] = payload_msg.get("tool_calls") or []

    if not raw_tool_calls or not payload_tool_calls:
        return

    # 构建 ID → 原始工具调用的映射表，用于高效匹配
    raw_by_id: dict[str, dict] = {}
    for raw_tc in raw_tool_calls:
        tc_id = raw_tc.get("id")
        if tc_id:
            raw_by_id[tc_id] = raw_tc

    for idx, payload_tc in enumerate(payload_tool_calls):
        # 优先按 ID 匹配，失败时回退到位置匹配
        raw_tc = raw_by_id.get(payload_tc.get("id", ""))
        if raw_tc is None and idx < len(raw_tool_calls):
            raw_tc = raw_tool_calls[idx]

        if raw_tc is None:
            continue

        # 网关可能使用 snake_case 或 camelCase 格式的签名键名
        sig = raw_tc.get("thought_signature") or raw_tc.get("thoughtSignature")
        if sig:
            payload_tc["thought_signature"] = sig
