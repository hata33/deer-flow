"""Token 用量追踪配置。

控制是否启用 TokenUsageMiddleware，该中间件记录每次 LLM 调用的 token 使用量。
token 数据存储在运行记录中，可通过 API 查询聚合统计。

启用后，子代理的 token 使用量也会被追踪：
- 子代理 usage 通过 tool_call_id 缓存
- 在分发 AIMessage 时按消息位置合并回主代理的统计

本配置是 AppConfig 的直接字段（不是全局单例），因为结构简单。
"""

from pydantic import BaseModel, Field


class TokenUsageConfig(BaseModel):
    """Token 用量追踪配置。

    - enabled: 是否启用 token 使用量追踪中间件
    """

    enabled: bool = Field(default=True, description="Enable token usage tracking middleware")
