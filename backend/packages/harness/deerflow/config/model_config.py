"""模型配置 — LLM 模型声明与能力标记。

每个 ModelConfig 实例声明一个可用的 LLM 模型，包含：
- 基础信息：名称、描述、模型标识
- Provider 路径：use 字段指定 langchain 模型类的完整路径
- 能力标记：thinking（扩展思考）、vision（图像理解）
- 条件配置：when_thinking_enabled/disabled 用于按 thinking 状态切换模型参数

本配置不负责创建模型实例——那是 models/factory.py 的工作。
这里只声明"有什么模型"和"模型有什么能力"。
"""

from pydantic import BaseModel, ConfigDict, Field


class ModelConfig(BaseModel):
    """单个 LLM 模型的配置。

    ### 基础字段
    - name: 模型唯一标识符，用于 Agent 运行时的模型选择
    - use: langchain 模型类的完整路径（如 langchain_openai.ChatOpenAI）
    - model: 传递给 Provider 的模型名称（如 gpt-4o、claude-3-opus）

    ### 能力标记
    - supports_thinking: 模型是否支持扩展思考（reasoning effort）
    - supports_vision: 模型是否支持图像输入

    ### 条件配置
    当 thinking 切换时，可以动态调整模型参数：
    - when_thinking_enabled: thinking 开启时追加的参数
    - when_thinking_disabled: thinking 关闭时追加的参数
    - thinking: thinking 参数的简写形式，与 when_thinking_enabled 合并

    ### OpenAI 特定
    - use_responses_api: 是否使用 OpenAI /v1/responses API
    - output_version: 结构化输出版本（如 responses/v1）

    extra="allow" 允许 Provider 特定的额外字段直接透传到模型构造函数。
    """

    name: str = Field(..., description="Unique name for the model")
    display_name: str | None = Field(..., default_factory=lambda: None, description="Display name for the model")
    description: str | None = Field(..., default_factory=lambda: None, description="Description for the model")
    use: str = Field(
        ...,
        description="Class path of the model provider(e.g. langchain_openai.ChatOpenAI)",
    )
    model: str = Field(..., description="Model name")
    model_config = ConfigDict(extra="allow")
    use_responses_api: bool | None = Field(
        default=None,
        description="Whether to route OpenAI ChatOpenAI calls through the /v1/responses API",
    )
    output_version: str | None = Field(
        default=None,
        description="Structured output version for OpenAI responses content, e.g. responses/v1",
    )
    supports_thinking: bool = Field(default_factory=lambda: False, description="Whether the model supports thinking")
    supports_reasoning_effort: bool = Field(default_factory=lambda: False, description="Whether the model supports reasoning effort")
    when_thinking_enabled: dict | None = Field(
        default_factory=lambda: None,
        description="Extra settings to be passed to the model when thinking is enabled",
    )
    when_thinking_disabled: dict | None = Field(
        default_factory=lambda: None,
        description="Extra settings to be passed to the model when thinking is disabled",
    )
    supports_vision: bool = Field(default_factory=lambda: False, description="Whether the model supports vision/image inputs")
    thinking: dict | None = Field(
        default_factory=lambda: None,
        description=(
            "Thinking settings for the model. If provided, these settings will be passed to the model when thinking is enabled. "
            "This is a shortcut for `when_thinking_enabled` and will be merged with `when_thinking_enabled` if both are provided."
        ),
    )
