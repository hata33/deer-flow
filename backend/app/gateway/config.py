"""API Gateway 配置模块。

管理网关的启动参数，包括监听地址、端口号和文档端点开关。
配置通过环境变量加载，支持运行时覆盖。

环境变量映射：
  - GATEWAY_HOST       — 监听地址（默认 "0.0.0.0"）
  - GATEWAY_PORT       — 监听端口（默认 8001）
  - GATEWAY_ENABLE_DOCS — 是否启用 /docs、/redoc、/openapi.json（默认 "true"）

设计要点：
  - 使用 Pydantic BaseModel 确保类型安全
  - 单例模式缓存配置实例，避免重复解析环境变量
  - 文档端点可通过环境变量关闭，适用于生产环境安全加固
"""

import os

from pydantic import BaseModel, Field


class GatewayConfig(BaseModel):
    """API Gateway 配置模型。

    Attributes:
        host: 网关服务器绑定的主机地址。
        port: 网关服务器绑定的端口号。
        enable_docs: 是否启用 Swagger/ReDoc/OpenAPI 文档端点。
    """

    host: str = Field(default="0.0.0.0", description="Host to bind the gateway server")
    port: int = Field(default=8001, description="Port to bind the gateway server")
    enable_docs: bool = Field(default=True, description="Enable Swagger/ReDoc/OpenAPI endpoints")


# 缓存的单例配置实例
_gateway_config: GatewayConfig | None = None


def get_gateway_config() -> GatewayConfig:
    """获取网关配置，首次调用时从环境变量加载。

    使用全局单例模式，后续调用直接返回缓存实例。

    Returns:
        GatewayConfig 实例。
    """
    global _gateway_config
    if _gateway_config is None:
        _gateway_config = GatewayConfig(
            host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
            port=int(os.getenv("GATEWAY_PORT", "8001")),
            enable_docs=os.getenv("GATEWAY_ENABLE_DOCS", "true").lower() == "true",
        )
    return _gateway_config
