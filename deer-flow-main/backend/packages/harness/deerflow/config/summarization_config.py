"""对话摘要（Summarization）配置。

本模块定义了 DeerFlow 自动对话摘要系统的配置。
当对话上下文接近 token 限制时，自动将较早的消息摘要为简短摘要，
保留最近的消息以维持对话连贯性。

工作原理：
    1. SummarizationMiddleware 在每次模型调用前检查上下文大小。
    2. 如果达到触发条件（trigger），启动摘要流程。
    3. 使用 LLM 将较旧的消息压缩为摘要。
    4. 保留最近的消息（由 keep 参数控制）。
    5. 摘要消息替换原始历史消息，释放 token 空间。

上下文大小类型：
    - **messages** — 按消息数量衡量（如保留最近 20 条消息）。
    - **tokens** — 按 token 数量衡量（如触发阈值为 4000 tokens）。
    - **fraction** — 按模型最大输入 token 的比例（如 0.8 = 80%）。

触发条件（trigger）：
    支持单个条件或多个条件（任一满足即触发）。

保留策略（keep）：
    摘要后保留的最近消息数量。

配置示例（config.yaml）：
    ```yaml
    summarization:
      enabled: true
      model_name: null              # 使用默认模型
      trigger:
        - type: messages
          value: 50
        - type: fraction
          value: 0.8
      keep:
        type: messages
        value: 20
      trim_tokens_to_summarize: 4000
    ```
"""
from typing import Literal

from pydantic import BaseModel, Field

# 上下文大小度量类型
ContextSizeType = Literal["fraction", "tokens", "messages"]


class ContextSize(BaseModel):
    """上下文大小规格。

    用于定义触发条件和保留策略的大小度量。

    Attributes:
        type: 度量类型。
            - 'messages': 按消息数量
            - 'tokens': 按 token 数量
            - 'fraction': 按模型最大输入 token 的比例
        value: 度量值。
            - messages/tokens: 整数
            - fraction: 0.0~1.0 之间的浮点数
    """

    type: ContextSizeType = Field(description="度量类型")
    value: int | float = Field(description="度量值")

    def to_tuple(self) -> tuple[ContextSizeType, int | float]:
        """转换为 SummarizationMiddleware 期望的元组格式。"""
        return (self.type, self.value)


class SummarizationConfig(BaseModel):
    """自动对话摘要配置。

    Attributes:
        enabled: 是否启用自动摘要。
        model_name: 摘要使用的模型名称。None 使用轻量模型。
        trigger: 触发条件。支持单个或多个条件（任一满足即触发）。
        keep: 摘要后的保留策略（保留多少最近消息）。
        trim_tokens_to_summarize: 准备摘要消息时的最大 token 数。
            null 跳过裁剪。
        summary_prompt: 自定义摘要提示词模板。
            null 使用 LangChain 默认提示词。
    """

    enabled: bool = Field(
        default=False,
        description="是否启用自动对话摘要",
    )
    model_name: str | None = Field(
        default=None,
        description="摘要使用的模型名称（None = 使用轻量模型）",
    )
    trigger: ContextSize | list[ContextSize] | None = Field(
        default=None,
        description="触发条件。支持单个或多个条件（任一满足即触发）。"
        "如 {'type': 'messages', 'value': 50} 在 50 条消息时触发，"
        "{'type': 'tokens', 'value': 4000} 在 4000 tokens 时触发，"
        "{'type': 'fraction', 'value': 0.8} 在模型最大输入的 80% 时触发。",
    )
    keep: ContextSize = Field(
        default_factory=lambda: ContextSize(type="messages", value=20),
        description="摘要后的保留策略。"
        "如 {'type': 'messages', 'value': 20} 保留 20 条消息，"
        "{'type': 'tokens', 'value': 3000} 保留 3000 tokens，"
        "{'type': 'fraction', 'value': 0.3} 保留模型最大输入的 30%。",
    )
    trim_tokens_to_summarize: int | None = Field(
        default=4000,
        description="准备摘要消息时的最大 token 数。null 跳过裁剪。",
    )
    summary_prompt: str | None = Field(
        default=None,
        description="自定义摘要提示词模板。null 使用 LangChain 默认提示词。",
    )


# ── 全局配置实例 ──────────────────────────────────────────────────────────
_summarization_config: SummarizationConfig = SummarizationConfig()


def get_summarization_config() -> SummarizationConfig:
    """获取当前摘要配置。"""
    return _summarization_config


def set_summarization_config(config: SummarizationConfig) -> None:
    """直接设置摘要配置。"""
    global _summarization_config
    _summarization_config = config


def load_summarization_config_from_dict(config_dict: dict) -> None:
    """从字典加载摘要配置（由 AppConfig.from_file 调用）。"""
    global _summarization_config
    _summarization_config = SummarizationConfig(**config_dict)
