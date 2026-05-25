"""MCP（Model Context Protocol）集成模块 —— 通过 langchain-mcp-adapters 对接外部工具服务。

本模块是 DeerFlow 与 MCP 生态的桥梁，负责：
  - 从 extensions_config.json 读取 MCP 服务器配置
  - 构建 MCP 客户端连接参数（支持 stdio / SSE / HTTP 三种传输方式）
  - 管理 OAuth 令牌的获取、缓存和自动刷新
  - 懒加载并缓存 MCP 工具，支持配置热更新检测
  - 将异步 MCP 工具包装为同步可调用形式，兼容 DeerFlow 嵌入式客户端

模块结构:
  - cache.py:   工具缓存管理（懒加载、mtime 热更新检测）
  - client.py:  MCP 客户端配置构建（传输参数映射）
  - oauth.py:   OAuth 令牌管理（获取/缓存/刷新/拦截器注入）
  - tools.py:   工具加载入口（整合客户端、OAuth、拦截器、同步包装）

使用方式:
    from deerflow.mcp import get_cached_mcp_tools, initialize_mcp_tools
"""

# 从各子模块导出公开 API
from .cache import get_cached_mcp_tools, initialize_mcp_tools, reset_mcp_tools_cache
from .client import build_server_params, build_servers_config
from .tools import get_mcp_tools

__all__ = [
    "build_server_params",
    "build_servers_config",
    "get_mcp_tools",
    "initialize_mcp_tools",
    "get_cached_mcp_tools",
    "reset_mcp_tools_cache",
]
