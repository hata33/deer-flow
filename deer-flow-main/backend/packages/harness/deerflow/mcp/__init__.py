"""MCP（Model Context Protocol）集成模块。

通过 langchain-mcp-adapters 连接外部 MCP 服务器，
将其提供的工具转换为 LangChain BaseTool 供 Agent 使用。

支持三种传输方式：stdio（命令行）、SSE（Server-Sent Events）、HTTP。
支持 OAuth 认证（client_credentials 和 refresh_token 流程）。

主要组件：
- client: 构建 MCP 服务器连接参数
- tools: 从 MCP 服务器加载工具并转为 LangChain 工具
- cache: 工具缓存与 mtime 变更检测
- oauth: OAuth token 管理
"""

from .cache import get_cached_mcp_tools, initialize_mcp_tools, reset_mcp_tools_cache
from .client import build_server_params, build_servers_config
from .tools import get_mcp_tools

__all__ = [
    "build_server_params",      # 构建单个 MCP 服务器参数
    "build_servers_config",     # 构建所有已启用服务器的配置
    "get_mcp_tools",            # 从 MCP 服务器异步加载工具
    "initialize_mcp_tools",     # 初始化并缓存 MCP 工具（应用启动时调用）
    "get_cached_mcp_tools",     # 获取缓存的 MCP 工具（含懒初始化和过期检测）
    "reset_mcp_tools_cache",    # 重置 MCP 工具缓存
]
