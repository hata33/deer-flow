"""延迟工具加载（Tool Search）配置。

本模块定义了 DeerFlow 的延迟工具加载配置。
启用后，MCP 工具不会直接加载到代理的上下文中，
而是通过 tool_search 工具在运行时按需发现和加载。

工作原理：
    传统模式：所有 MCP 工具在启动时加载到代理的工具列表中。
    tool_search 模式：
    1. MCP 工具仅以名称列表的形式出现在系统提示词中。
    2. 代理通过调用 tool_search 工具来搜索和发现可用工具。
    3. tool_search 返回工具的详细描述和调用方式。
    4. 代理按需调用具体工具。

适用场景：
    - MCP 服务器提供大量工具时，减少初始 prompt token 消耗。
    - 工具按需加载，降低启动时间和内存占用。

配置示例（config.yaml）：
    ```yaml
    tool_search:
      enabled: true
    ```

注意：
    - 默认关闭（enabled: false）。
    - 启用后代理的 tool_search 工具可用，用于运行时工具发现。
"""
from pydantic import BaseModel, Field


class ToolSearchConfig(BaseModel):
    """延迟工具加载配置。

    Attributes:
        enabled: 是否启用延迟工具加载。
            启用后 MCP 工具不直接加载，而是通过 tool_search 按需发现。
    """

    enabled: bool = Field(
        default=False,
        description="启用延迟工具加载和 tool_search",
    )


# ── 全局配置实例 ──────────────────────────────────────────────────────────
_tool_search_config: ToolSearchConfig | None = None


def get_tool_search_config() -> ToolSearchConfig:
    """获取当前 tool_search 配置。

    如果尚未加载，返回默认配置（enabled=False）。
    """
    global _tool_search_config
    if _tool_search_config is None:
        _tool_search_config = ToolSearchConfig()
    return _tool_search_config


def load_tool_search_config_from_dict(data: dict) -> ToolSearchConfig:
    """从字典加载 tool_search 配置（由 AppConfig.from_file 调用）。

    Args:
        data: config.yaml 中 tool_search 字段的字典。

    Returns:
        加载后的 ToolSearchConfig 实例。
    """
    global _tool_search_config
    _tool_search_config = ToolSearchConfig.model_validate(data)
    return _tool_search_config
