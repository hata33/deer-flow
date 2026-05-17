"""模型配置定义。

本模块定义了 DeerFlow 中 LLM 模型的配置结构（ModelConfig），
用于描述模型的基本信息、提供商路径以及能力标识。

能力标识说明：
    - **thinking** — 模型是否支持扩展思考（extended thinking），启用后模型在回复前会进行内部推理。
    - **vision** — 模型是否支持图像输入（多模态），启用后 ViewImageMiddleware 会注入 base64 图片。
    - **reasoning_effort** — 模型是否支持推理努力级别调节（如 low/medium/high）。

构造参数说明：
    - **use** — 模型提供商的类路径，通过反射系统（resolve_variable）动态加载。
      例如 ``langchain_openai.ChatOpenAI`` 表示使用 OpenAI 的 Chat 模型。
    - **model** — 模型标识名，传递给提供商的 model 参数（如 ``gpt-4o``）。
    - **use_responses_api** — 是否将 OpenAI ChatOpenAI 调用路由到 ``/v1/responses`` API。
    - **output_version** — OpenAI responses API 的结构化输出版本（如 ``responses/v1``）。
    - **when_thinking_enabled** — 当 thinking 启用时，额外传递给模型的参数字典。
    - **thinking** — thinking 参数的快捷方式，会与 when_thinking_enabled 合并。

配置示例（config.yaml）：
    ```yaml
    models:
      - name: openai-gpt4o
        display_name: GPT-4o
        description: OpenAI GPT-4o 模型
        use: langchain_openai.ChatOpenAI
        model: gpt-4o
        supports_thinking: false
        supports_vision: true
      - name: openai-o3
        use: langchain_openai.ChatOpenAI
        model: o3
        supports_thinking: true
        supports_reasoning_effort: true
        thinking:
          budget_tokens: 10000
    ```

注意：
    - ``model_config = ConfigDict(extra="allow")`` 允许配置文件中传入额外的提供商特定参数，
      这些参数会在 create_chat_model() 时直接传递给模型构造函数。
    - ``use_responses_api`` 和 ``output_version`` 仅在使用 langchain_openai.ChatOpenAI 时有效。
"""
from pydantic import BaseModel, ConfigDict, Field


class ModelConfig(BaseModel):
    """模型配置项。

    对应 config.yaml 中 ``models[]`` 列表中的单个模型定义。
    所有 ``Field`` 的 description 会作为配置文件的文档注释。
    """

    # ── 基本信息 ──────────────────────────────────────────────────────────

    name: str = Field(..., description="模型唯一标识名，用于在运行时选择模型")
    display_name: str | None = Field(..., default_factory=lambda: None, description="模型的显示名称（用于前端展示）")
    description: str | None = Field(..., default_factory=lambda: None, description="模型的描述信息")

    # ── 提供商配置 ────────────────────────────────────────────────────────

    use: str = Field(
        ...,
        description="模型提供商的类路径，通过反射系统加载（如 langchain_openai.ChatOpenAI）",
    )
    model: str = Field(..., description="传递给提供商的模型标识名（如 gpt-4o、claude-3-opus）")

    # 允许传入额外的提供商特定参数（如 temperature、max_tokens 等）
    model_config = ConfigDict(extra="allow")

    # ── OpenAI Responses API 配置 ─────────────────────────────────────────

    use_responses_api: bool | None = Field(
        default=None,
        description="是否将 OpenAI ChatOpenAI 调用路由到 /v1/responses API（仅 OpenAI 有效）",
    )
    output_version: str | None = Field(
        default=None,
        description="OpenAI responses API 的结构化输出版本（如 responses/v1）",
    )

    # ── 模型能力标识 ──────────────────────────────────────────────────────

    supports_thinking: bool = Field(
        default_factory=lambda: False,
        description="模型是否支持扩展思考（extended thinking），启用后模型在回复前进行内部推理",
    )
    supports_reasoning_effort: bool = Field(
        default_factory=lambda: False,
        description="模型是否支持推理努力级别调节（如 low/medium/high）",
    )

    # ── Thinking 参数 ─────────────────────────────────────────────────────

    when_thinking_enabled: dict | None = Field(
        default_factory=lambda: None,
        description="当 thinking 启用时，额外传递给模型的参数字典",
    )
    supports_vision: bool = Field(
        default_factory=lambda: False,
        description="模型是否支持图像输入（多模态），启用后 ViewImageMiddleware 会注入 base64 图片",
    )
    thinking: dict | None = Field(
        default_factory=lambda: None,
        description=(
            "模型的 thinking 参数快捷方式。如果提供，这些参数会在 thinking 启用时传递给模型。"
            "这是 when_thinking_enabled 的快捷方式，两者同时提供时会合并。"
        ),
    )
