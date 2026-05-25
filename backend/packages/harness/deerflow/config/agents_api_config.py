"""自定义代理管理 API 配置。

控制是否通过 HTTP 暴露自定义代理的 CRUD 操作。
当启用时，Gateway API 提供：
- 自定义代理 SOUL.md 的读写
- 自定义代理 config.yaml 的读写
- USER.md 全局用户画像的管理

### 安全考虑
在多租户环境中，此 API 应配合认证中间件使用。
禁用时，所有代理和用户画像的读写路由返回拒绝。

本配置作为全局单例管理。
"""

from pydantic import BaseModel, Field


class AgentsApiConfig(BaseModel):
    """自定义代理管理 API 配置。

    - enabled: 是否启用代理管理 HTTP API
    """

    enabled: bool = Field(
        default=False,
        description=("Whether to expose the custom-agent management API over HTTP. When disabled, the gateway rejects read/write access to custom agent SOUL.md, config, and USER.md prompt-management routes."),
    )


# 全局单例 — 由 AppConfig._apply_singleton_configs() 更新
_agents_api_config: AgentsApiConfig = AgentsApiConfig()


def get_agents_api_config() -> AgentsApiConfig:
    """获取当前代理 API 配置（全局单例）。"""
    return _agents_api_config


def set_agents_api_config(config: AgentsApiConfig) -> None:
    """设置代理 API 配置。"""
    global _agents_api_config
    _agents_api_config = config


def load_agents_api_config_from_dict(config_dict: dict) -> None:
    """从字典加载代理 API 配置（由 AppConfig 初始化时调用）。"""
    global _agents_api_config
    _agents_api_config = AgentsApiConfig(**config_dict)
