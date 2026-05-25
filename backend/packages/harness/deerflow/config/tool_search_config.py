"""工具搜索配置 — MCP 工具延迟加载。

当工具数量很多时（特别是 MCP 工具），将所有工具的 schema 加载到 LLM 上下文中
会消耗大量 token。工具搜索功能通过延迟加载解决这个问题：

1. 不直接将 MCP 工具加载到 Agent 的上下文
2. 在系统提示词中列出可用工具的名称
3. Agent 通过 tool_search 工具在运行时按需加载特定工具

### 工作原理
- enabled=false（默认）: 所有工具直接加载到 Agent 上下文
- enabled=true: MCP 工具被隐藏，Agent 通过 tool_search 按需发现和加载

DeferredToolFilterMiddleware 负责在 Agent 与 LLM 交互前过滤掉被延迟的工具 schema。

本配置作为全局单例管理。
"""

from pydantic import BaseModel, Field


class ToolSearchConfig(BaseModel):
    """工具搜索 / 延迟加载配置。

    - enabled: 是否启用延迟加载模式
    """

    enabled: bool = Field(
        default=False,
        description="Defer tools and enable tool_search",
    )


# 全局单例 — 由 AppConfig._apply_singleton_configs() 更新
_tool_search_config: ToolSearchConfig | None = None


def get_tool_search_config() -> ToolSearchConfig:
    """获取当前工具搜索配置。

    未加载时返回默认配置（enabled=False）。
    """
    global _tool_search_config
    if _tool_search_config is None:
        _tool_search_config = ToolSearchConfig()
    return _tool_search_config


def load_tool_search_config_from_dict(data: dict) -> ToolSearchConfig:
    """从字典加载工具搜索配置（由 AppConfig 初始化时调用）。"""
    global _tool_search_config
    _tool_search_config = ToolSearchConfig.model_validate(data)
    return _tool_search_config
