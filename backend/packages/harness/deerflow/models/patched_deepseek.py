"""DeepSeek 补丁模型 — 修复多轮对话中 reasoning_content 丢失的问题。

模块功能
========
为 LangChain 的 ChatDeepSeek 提供补丁版本，确保在多轮对话中正确保留和传递
reasoning_content（推理内容）字段。原始实现将 reasoning_content 存储在
additional_kwargs 中，但在构建后续 API 请求时未将其包含在 payload 中，
导致启用了思维模式的 API 在处理后续请求时报错。

核心设计
========
1. **负载补丁**: 重写 `_get_request_payload` 方法，在构建 API 请求负载后，
   遍历所有 assistant 消息，将 additional_kwargs 中的 reasoning_content
   重新注入到对应的 payload 消息中。
2. **双模式匹配**: 优先使用精确位置匹配（payload 和原始消息一一对应），
   当消息数量不一致时回退到基于角色计数的匹配策略。
3. **最小侵入**: 仅修改请求负载构建逻辑，不改变消息存储和流式处理逻辑。

问题背景
========
DeepSeek API 在启用思维模式（thinking）后，要求所有 assistant 消息都携带
reasoning_content 字段。LangChain 的 ChatDeepSeek 虽然在接收响应时正确提取
了 reasoning_content 并存储在 additional_kwargs 中，但在构建后续请求时
只序列化了标准的消息字段，导致 reasoning_content 被静默丢弃。

使用场景
========
在 config.yaml 中配置 DeepSeek 思维模型::

    - name: deepseek-r1
      use: deerflow.models.patched_deepseek:PatchedChatDeepSeek
      model: deepseek-reasoner
      supports_thinking: true

注意事项
========
- 需要安装 langchain-deepseek 包
- 仅在启用思维模式时才会出现 reasoning_content 丢失的问题
- 补丁不影响非思维模式下的正常使用
"""

from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek


class PatchedChatDeepSeek(ChatDeepSeek):
    """修复 reasoning_content 保留问题的 ChatDeepSeek 补丁版本。

    当使用 DeepSeek 的思维/推理模式时，API 要求所有 assistant 消息在多轮对话中
    都携带 reasoning_content 字段。此补丁版本确保 additional_kwargs 中的
    reasoning_content 被正确注入到请求负载中。

    补丁原理：
    1. 在构建请求负载前，先保存原始的 LangChain 消息列表
    2. 调用父类方法获取标准 payload
    3. 遍历 payload 中的 assistant 消息，从对应的原始 AIMessage 中提取
       reasoning_content 并注入

    配置示例::

        - name: deepseek-r1
          use: deerflow.models.patched_deepseek:PatchedChatDeepSeek
          model: deepseek-reasoner
          supports_thinking: true
    """

    @classmethod
    def is_lc_serializable(cls) -> bool:
        """声明本类支持 LangChain 序列化。

        启用序列化后，模型实例可以被 LangGraph 的 checkpointer 正确保存和恢复。
        """
        return True

    @property
    def lc_secrets(self) -> dict[str, str]:
        """声明敏感字段及其对应的环境变量名。

        LangChain 序列化时会跳过这些字段，避免 API Key 泄露到日志或存储中。

        Returns:
            dict: 字段名到环境变量名的映射。
        """
        return {"api_key": "DEEPSEEK_API_KEY", "openai_api_key": "DEEPSEEK_API_KEY"}

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """构建包含 reasoning_content 的请求负载。

        在父类生成的标准负载基础上，遍历所有 assistant 消息，将原始 AIMessage
        中 additional_kwargs 保存的 reasoning_content 重新注入到 payload 对应
        的消息字典中。

        匹配策略：
        1. 优先精确匹配：当 payload 和原始消息数量一致时，按位置一一对应
        2. 回退匹配：数量不一致时，分别收集 assistant 角色的消息进行位置匹配

        Args:
            input_: LangChain 消息输入（消息列表或提示模板）。
            stop: 停止词列表（可选）。
            **kwargs: 其他传递给父类的关键字参数。

        Returns:
            dict: 包含 reasoning_content 的 API 请求负载字典。
        """
        # 在转换前捕获原始 LangChain 消息，以便访问被序列化器丢弃的字段
        original_messages = self._convert_input(input_).to_messages()

        # 调用父类获取标准 payload
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # 匹配 payload 消息与原始消息，恢复 reasoning_content
        payload_messages = payload.get("messages", [])

        # payload 消息和原始消息应该按相同顺序排列，按位置逐一匹配
        if len(payload_messages) == len(original_messages):
            for payload_msg, orig_msg in zip(payload_messages, original_messages):
                if payload_msg.get("role") == "assistant" and isinstance(orig_msg, AIMessage):
                    reasoning_content = orig_msg.additional_kwargs.get("reasoning_content")
                    if reasoning_content is not None:
                        payload_msg["reasoning_content"] = reasoning_content
        else:
            # 回退方案：通过计数 assistant 消息进行位置匹配
            # 这种情况发生在父类对消息进行了过滤或重组时
            ai_messages = [m for m in original_messages if isinstance(m, AIMessage)]
            assistant_payloads = [(i, m) for i, m in enumerate(payload_messages) if m.get("role") == "assistant"]

            for (idx, payload_msg), ai_msg in zip(assistant_payloads, ai_messages):
                reasoning_content = ai_msg.additional_kwargs.get("reasoning_content")
                if reasoning_content is not None:
                    payload_messages[idx]["reasoning_content"] = reasoning_content

        return payload
