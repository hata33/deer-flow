"""Token 用量追踪配置。

本模块定义了 DeerFlow 的 Token 用量追踪功能配置。
启用后，系统会记录每次 LLM 调用的 token 消耗情况。

工作原理：
    启用后，TokenUsageMiddleware 会在每次模型调用后记录
    prompt_tokens、completion_tokens 和 total_tokens。
    这些数据可用于成本分析和用量监控。

配置示例（config.yaml）：
    ```yaml
    token_usage:
      enabled: true
    ```

注意：
    - 该配置嵌入在 AppConfig 中（非独立全局单例）。
    - 默认关闭（enabled: false），需要显式启用。
"""
from pydantic import BaseModel, Field


class TokenUsageConfig(BaseModel):
    """Token 用量追踪配置。

    Attributes:
        enabled: 是否启用 Token 用量追踪中间件。
            启用后会记录每次 LLM 调用的 token 消耗。
    """

    enabled: bool = Field(default=False, description="是否启用 Token 用量追踪中间件")
