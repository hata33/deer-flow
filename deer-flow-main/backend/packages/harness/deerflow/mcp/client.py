"""MCP 客户端配置构建器。

将 extensions_config.json 中的 MCP 服务器配置转换为
langchain-mcp-adapters 的 MultiServerMCPClient 所需的参数格式。

支持三种传输方式：
- stdio: 通过命令行启动本地进程，需要 command 和 args
- sse: 通过 Server-Sent Events 连接远程服务器，需要 url
- http: 通过 HTTP 连接远程服务器，需要 url
"""

import logging
from typing import Any

from deerflow.config.extensions_config import ExtensionsConfig, McpServerConfig

logger = logging.getLogger(__name__)


def build_server_params(server_name: str, config: McpServerConfig) -> dict[str, Any]:
    """构建单个 MCP 服务器的连接参数。

    根据传输类型（stdio/sse/http）生成对应的参数字典，
    包含命令、参数、环境变量或 URL、请求头等。

    Args:
        server_name: MCP 服务器名称。
        config: MCP 服务器配置。

    Returns:
        服务器参数字典。

    Raises:
        ValueError: 传输类型不支持或缺少必要字段。
    """
    transport_type = config.type or "stdio"
    params: dict[str, Any] = {"transport": transport_type}

    if transport_type == "stdio":
        if not config.command:
            raise ValueError(f"MCP server '{server_name}' with stdio transport requires 'command' field")
        params["command"] = config.command
        params["args"] = config.args
        # 注入环境变量
        if config.env:
            params["env"] = config.env
    elif transport_type in ("sse", "http"):
        if not config.url:
            raise ValueError(f"MCP server '{server_name}' with {transport_type} transport requires 'url' field")
        params["url"] = config.url
        # 注入自定义请求头
        if config.headers:
            params["headers"] = config.headers
    else:
        raise ValueError(f"MCP server '{server_name}' has unsupported transport type: {transport_type}")

    return params


def build_servers_config(extensions_config: ExtensionsConfig) -> dict[str, dict[str, Any]]:
    """构建所有已启用 MCP 服务器的配置字典。

    遍历已启用的服务器，逐一构建连接参数。
    单个服务器构建失败时记录错误但不影响其他服务器。

    Args:
        extensions_config: 扩展配置，包含 MCP 服务器定义。

    Returns:
        服务器名到参数字典的映射。
    """
    enabled_servers = extensions_config.get_enabled_mcp_servers()

    if not enabled_servers:
        logger.info("No enabled MCP servers found")
        return {}

    servers_config = {}
    for server_name, server_config in enabled_servers.items():
        try:
            servers_config[server_name] = build_server_params(server_name, server_config)
            logger.info(f"Configured MCP server: {server_name}")
        except Exception as e:
            logger.error(f"Failed to configure MCP server '{server_name}': {e}")

    return servers_config
