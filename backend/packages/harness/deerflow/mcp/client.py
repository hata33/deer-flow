"""MCP 客户端配置构建 —— 将 DeerFlow 配置映射为 langchain-mcp-adapters 参数。

本模块负责将 extensions_config.json 中的 MCP 服务器声明式配置
转换为 langchain-mcp-adapters 的 MultiServerMCPClient 所需的参数字典。

核心职责:
  - build_server_params:  单个服务器的参数构建（传输方式分支处理）
  - build_servers_config: 批量构建所有已启用服务器的配置

传输方式映射:
  - stdio:  启动本地子进程作为 MCP 服务器（command + args + env）
  - sse:    通过 Server-Sent Events 连接远程 MCP 服务器（url + headers）
  - http:   通过 HTTP 流式传输连接远程 MCP 服务器（url + headers）

为什么需要这个模块:
  langchain-mcp-adapters 期望特定格式的参数字典，
  而 extensions_config.json 使用 Pydantic 模型（McpServerConfig）。
  本模块在两者之间做适配转换，同时进行配置校验（如缺少必要字段时报错）。
"""

import logging
from typing import Any

from deerflow.config.extensions_config import ExtensionsConfig, McpServerConfig

logger = logging.getLogger(__name__)


def build_server_params(server_name: str, config: McpServerConfig) -> dict[str, Any]:
    """为单个 MCP 服务器构建 langchain-mcp-adapters 所需的连接参数。

    根据传输类型（transport type）构建不同结构的参数字典：

    - stdio 传输：
        需要提供 command（启动命令），可选 args（命令参数）和 env（环境变量）。
        langchain-mcp-adapters 会启动一个子进程，通过 stdin/stdout 与其通信。

    - sse/http 传输：
        需要提供 url（服务器地址），可选 headers（HTTP 请求头）。
        langchain-mcp-adapters 通过 HTTP/SSE 协议与远程服务器通信。
        headers 通常用于传递认证信息（如 API Key、OAuth Token）。

    Args:
        server_name: 服务器名称（用于日志和错误信息）
        config:      MCP 服务器的 Pydantic 配置对象

    Returns:
        langchain-mcp-adapters 可接受的参数字典，例如：
          - stdio:  {"transport": "stdio", "command": "npx", "args": ["-y", "some-mcp-server"]}
          - sse:    {"transport": "sse", "url": "https://example.com/mcp", "headers": {...}}

    Raises:
        ValueError: 配置缺少必要字段（如 stdio 缺少 command、sse 缺少 url）
    """
    transport_type = config.type or "stdio"
    params: dict[str, Any] = {"transport": transport_type}

    if transport_type == "stdio":
        # stdio 传输：启动本地子进程
        # command 是必须的，否则无法知道要启动什么进程
        if not config.command:
            raise ValueError(f"MCP server '{server_name}' with stdio transport requires 'command' field")
        params["command"] = config.command
        params["args"] = config.args
        # 环境变量：用于向 MCP 服务器进程传递 API Key 等敏感信息
        if config.env:
            params["env"] = config.env

    elif transport_type in ("sse", "http"):
        # SSE/HTTP 传输：连接远程服务器
        # url 是必须的，否则无法知道连接地址
        if not config.url:
            raise ValueError(f"MCP server '{server_name}' with {transport_type} transport requires 'url' field")
        params["url"] = config.url
        # 自定义 HTTP 头：用于传递认证信息、自定义元数据等
        if config.headers:
            params["headers"] = config.headers

    else:
        raise ValueError(f"MCP server '{server_name}' has unsupported transport type: {transport_type}")

    return params


def build_servers_config(extensions_config: ExtensionsConfig) -> dict[str, dict[str, Any]]:
    """批量构建所有已启用 MCP 服务器的配置字典。

    遍历 extensions_config 中所有 enabled=true 的服务器，
    逐一构建连接参数。单个服务器构建失败不会影响其他服务器，
    错误会记录到日志。

    为什么单服务器失败不影响全局：
      MCP 服务器由不同团队/供应商提供，某个服务器配置错误或不可用
      不应阻止其他正常服务器的工具加载。

    Args:
        extensions_config: 扩展配置对象，包含所有 MCP 服务器声明

    Returns:
        服务器名称到参数字典的映射，例如：
        {
            "filesystem": {"transport": "stdio", "command": "npx", ...},
            "remote-api": {"transport": "sse", "url": "https://...", ...},
        }
        如果没有已启用的服务器，返回空字典。
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
            # 单个服务器配置失败不影响其他服务器
            logger.error(f"Failed to configure MCP server '{server_name}': {e}")

    return servers_config
