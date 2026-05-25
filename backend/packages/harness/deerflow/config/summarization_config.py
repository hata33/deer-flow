"""对话摘要配置 — 上下文自动压缩。

当对话接近模型的 token 限制时，摘要系统自动将较早的对话内容压缩为摘要，
保留近期消息以维持上下文连贯性。

### 触发策略（trigger）
支持三种维度，可组合使用：
- messages: 消息数量触发（如 50 条消息后触发）
- tokens: token 数量触发（如 4000 tokens 后触发）
- fraction: 占模型最大输入的比例触发（如 80% 时触发）

当任意一个条件满足时，触发摘要。

### 保留策略（keep）
摘要后保留多少上下文，同样支持三种维度。

### 技能保留（skill preservation）
摘要时保留最近加载的技能文件，避免 Agent 丢失刚学习的技能内容。
通过跟踪 read_file/read/view/cat 等工具调用识别技能文件读取。

本配置作为全局单例，由 AppConfig 初始化时更新。
"""

from typing import Literal

from pydantic import BaseModel, Field

ContextSizeType = Literal["fraction", "tokens", "messages"]


class ContextSize(BaseModel):
    """上下文大小规格 — 统一的触发/保留策略描述。

    type 决定 value 的含义：
    - fraction: value 是 0-1 的比例（如 0.8 = 80%）
    - tokens: value 是绝对 token 数
    - messages: value 是消息条数
    """

    type: ContextSizeType = Field(description="Type of context size specification")
    value: int | float = Field(description="Value for the context size specification")

    def to_tuple(self) -> tuple[ContextSizeType, int | float]:
        """转换为元组格式，供 SummarizationMiddleware 使用。"""
        return (self.type, self.value)


class SummarizationConfig(BaseModel):
    """对话自动摘要配置。

    ### 核心字段
    - enabled: 是否启用（默认关闭）
    - model_name: 摘要使用的模型（None = 使用轻量模型）
    - trigger: 触发条件（可以是单个或多个）
    - keep: 摘要后保留的上下文量

    ### 摘要优化
    - trim_tokens_to_summarize: 准备摘要时的 token 上限（防止摘要本身过长）
    - summary_prompt: 自定义摘要提示词

    ### 技能保留
    - preserve_recent_skill_count: 保留最近 N 个技能文件
    - preserve_recent_skill_tokens: 保留技能的总 token 预算
    - preserve_recent_skill_tokens_per_skill: 单个技能文件的 token 上限
    - skill_file_read_tool_names: 被识别为"技能文件读取"的工具名列表
    """

    enabled: bool = Field(
        default=False,
        description="Whether to enable automatic conversation summarization",
    )
    model_name: str | None = Field(
        default=None,
        description="Model name to use for summarization (None = use a lightweight model)",
    )
    trigger: ContextSize | list[ContextSize] | None = Field(
        default=None,
        description="One or more thresholds that trigger summarization. When any threshold is met, summarization runs. "
        "Examples: {'type': 'messages', 'value': 50} triggers at 50 messages, "
        "{'type': 'tokens', 'value': 4000} triggers at 4000 tokens, "
        "{'type': 'fraction', 'value': 0.8} triggers at 80% of model's max input tokens",
    )
    keep: ContextSize = Field(
        default_factory=lambda: ContextSize(type="messages", value=20),
        description="Context retention policy after summarization. Specifies how much history to preserve. "
        "Examples: {'type': 'messages', 'value': 20} keeps 20 messages, "
        "{'type': 'tokens', 'value': 3000} keeps 3000 tokens, "
        "{'type': 'fraction', 'value': 0.3} keeps 30% of model's max input tokens",
    )
    trim_tokens_to_summarize: int | None = Field(
        default=4000,
        description="Maximum tokens to keep when preparing messages for summarization. Pass null to skip trimming.",
    )
    summary_prompt: str | None = Field(
        default=None,
        description="Custom prompt template for generating summaries. If not provided, uses the default LangChain prompt.",
    )
    preserve_recent_skill_count: int = Field(
        default=5,
        ge=0,
        description="Number of most-recently-loaded skill files to exclude from summarization. Set to 0 to disable skill preservation.",
    )
    preserve_recent_skill_tokens: int = Field(
        default=25000,
        ge=0,
        description="Total token budget reserved for recently-loaded skill files that must be preserved across summarization.",
    )
    preserve_recent_skill_tokens_per_skill: int = Field(
        default=5000,
        ge=0,
        description="Per-skill token cap when preserving skill files across summarization. Skill reads above this size are not rescued.",
    )
    skill_file_read_tool_names: list[str] = Field(
        default_factory=lambda: ["read_file", "read", "view", "cat"],
        description="Tool names treated as skill file reads when preserving recently-loaded skills across summarization.",
    )


# 全局单例 — 由 AppConfig._apply_singleton_configs() 更新
_summarization_config: SummarizationConfig = SummarizationConfig()


def get_summarization_config() -> SummarizationConfig:
    """获取当前摘要配置（全局单例）。"""
    return _summarization_config


def set_summarization_config(config: SummarizationConfig) -> None:
    """设置摘要配置。"""
    global _summarization_config
    _summarization_config = config


def load_summarization_config_from_dict(config_dict: dict) -> None:
    """从字典加载摘要配置（由 AppConfig 初始化时调用）。"""
    global _summarization_config
    _summarization_config = SummarizationConfig(**config_dict)
