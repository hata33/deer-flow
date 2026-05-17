"""自动标题生成（Title）配置。

本模块定义了 DeerFlow 线程自动标题生成的配置。
在首次完整对话（用户消息 + AI 回复）后，系统使用 LLM 自动生成线程标题。

工作原理：
    1. TitleMiddleware 在首次完整对话后触发。
    2. 使用配置的模型（或默认模型）调用 LLM 生成标题。
    3. 标题受字数和字符数限制。
    4. 生成的标题保存在 ThreadState.title 中。

提示词模板：
    默认模板接受 {user_msg}、{assistant_msg}、{max_words} 三个变量。
    可以通过 prompt_template 自定义标题生成提示词。

配置示例（config.yaml）：
    ```yaml
    title:
      enabled: true
      max_words: 6
      max_chars: 60
      model_name: null
    ```
"""
from pydantic import BaseModel, Field


class TitleConfig(BaseModel):
    """自动标题生成配置。

    Attributes:
        enabled: 是否启用自动标题生成。
        max_words: 标题最大字数（1~20）。
        max_chars: 标题最大字符数（10~200）。
        model_name: 标题生成使用的模型名称。None 使用默认模型。
        prompt_template: 标题生成提示词模板。
            可用变量：{user_msg}、{assistant_msg}、{max_words}。
    """

    enabled: bool = Field(
        default=True,
        description="是否启用自动标题生成",
    )
    max_words: int = Field(
        default=6,
        ge=1,
        le=20,
        description="标题最大字数",
    )
    max_chars: int = Field(
        default=60,
        ge=10,
        le=200,
        description="标题最大字符数",
    )
    model_name: str | None = Field(
        default=None,
        description="标题生成使用的模型名称（None = 使用默认模型）",
    )
    prompt_template: str = Field(
        default=("Generate a concise title (max {max_words} words) for this conversation.\nUser: {user_msg}\nAssistant: {assistant_msg}\n\nReturn ONLY the title, no quotes, no explanation."),
        description="标题生成提示词模板",
    )


# ── 全局配置实例 ──────────────────────────────────────────────────────────
_title_config: TitleConfig = TitleConfig()


def get_title_config() -> TitleConfig:
    """获取当前标题配置。"""
    return _title_config


def set_title_config(config: TitleConfig) -> None:
    """直接设置标题配置。"""
    global _title_config
    _title_config = config


def load_title_config_from_dict(config_dict: dict) -> None:
    """从字典加载标题配置（由 AppConfig.from_file 调用）。"""
    global _title_config
    _title_config = TitleConfig(**config_dict)
