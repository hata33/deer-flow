"""
模型配置

定义 LLM 模型的配置。
"""
from typing import Any

from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    """LLM 模型配置"""

    name: str = Field(description="模型名称")
    provider: str = Field(default="openai", description="提供商：openai, anthropic, 等")
    api_key: str | None = Field(default=None, description="API 密钥")
    base_url: str | None = Field(default=None, description="API 基础 URL")
    model: str = Field(description="模型标识符，如 gpt-4, claude-3-opus-20240229")

    # 功能标志
    supports_vision: bool = Field(default=False, description="是否支持视觉")
    supports_tools: bool = Field(default=True, description="是否支持工具调用")

    # 高级参数
    temperature: float = Field(default=0.7, description="温度参数", ge=0, le=2)
    max_tokens: int | None = Field(default=None, description="最大生成 token 数")

    # 其他参数
    extra_params: dict[str, Any] = Field(default_factory=dict, description="其他参数")

    def get_init_kwargs(self) -> dict[str, Any]:
        """获取模型初始化参数"""
        kwargs = {
            "model": self.model,
            "temperature": self.temperature,
        }

        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens

        if self.api_key:
            kwargs["api_key"] = self.api_key

        if self.base_url:
            kwargs["base_url"] = self.base_url

        kwargs.update(self.extra_params)
        return kwargs
