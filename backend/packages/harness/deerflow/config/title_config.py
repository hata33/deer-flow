"""标题生成配置 — 对话标题自动生成。

在首次完整对话交换后，系统自动使用 LLM 为线程生成一个简洁的标题。
标题用于 UI 中对话列表的显示。

### 生成时机
由 TitleMiddleware 控制，在以下条件满足时触发：
1. 标题功能已启用
2. 当前线程尚无标题
3. 已完成至少一轮完整的用户-AI 交换

### 生成模型
默认使用主模型，但可以通过 model_name 指定使用更轻量的模型以节省成本。

本配置作为全局单例管理。
"""

from pydantic import BaseModel, Field


class TitleConfig(BaseModel):
    """自动标题生成配置。

    - enabled: 是否启用
    - max_words: 标题最大词数
    - max_chars: 标题最大字符数
    - model_name: 生成使用的模型（None = 默认模型）
    - prompt_template: 生成提示词模板，支持 {max_words}、{user_msg}、{assistant_msg} 占位符
    """

    enabled: bool = Field(
        default=True,
        description="Whether to enable automatic title generation",
    )
    max_words: int = Field(
        default=6,
        ge=1,
        le=20,
        description="Maximum number of words in the generated title",
    )
    max_chars: int = Field(
        default=60,
        ge=10,
        le=200,
        description="Maximum number of characters in the generated title",
    )
    model_name: str | None = Field(
        default=None,
        description="Model name to use for title generation (None = use default model)",
    )
    prompt_template: str = Field(
        default=("Generate a concise title (max {max_words} words) for this conversation.\nUser: {user_msg}\nAssistant: {assistant_msg}\n\nReturn ONLY the title, no quotes, no explanation."),
        description="Prompt template for title generation",
    )


# 全局单例 — 由 AppConfig._apply_singleton_configs() 更新
_title_config: TitleConfig = TitleConfig()


def get_title_config() -> TitleConfig:
    """获取当前标题配置（全局单例）。"""
    return _title_config


def set_title_config(config: TitleConfig) -> None:
    """设置标题配置。"""
    global _title_config
    _title_config = config


def load_title_config_from_dict(config_dict: dict) -> None:
    """从字典加载标题配置（由 AppConfig 初始化时调用）。"""
    global _title_config
    _title_config = TitleConfig(**config_dict)
