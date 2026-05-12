"""MCP 工具加载器。

通过 langchain-mcp-adapters 的 MultiServerMCPClient 从所有已启用的
MCP 服务器加载工具，并转换为 LangChain BaseTool。

关键设计：
- 每次从磁盘读取最新配置（ExtensionsConfig.from_file()），确保跨进程一致
- 为异步工具生成同步包装器，支持 DeerFlowClient 的同步流式调用
- 支持 OAuth token 注入到 HTTP/SSE 服务器连接和工具调用
"""

import asyncio
import atexit
import concurrent.futures
import logging
from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool

from deerflow.config.extensions_config import ExtensionsConfig
from deerflow.mcp.client import build_servers_config
from deerflow.mcp.oauth import build_oauth_tool_interceptor, get_initial_oauth_headers

logger = logging.getLogger(__name__)

# 全局线程池：用于在异步环境中执行同步工具调用
_SYNC_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="mcp-sync-tool")

# 注册进程退出时的清理钩子
atexit.register(lambda: _SYNC_TOOL_EXECUTOR.shutdown(wait=False))


def _make_sync_tool_wrapper(coro: Callable[..., Any], tool_name: str) -> Callable[..., Any]:
    """为异步工具协程构建同步包装器。

    处理嵌套事件循环场景：当已有事件循环运行时，
    通过全局线程池在新线程中创建独立循环执行异步调用。

    Args:
        coro: 工具的异步协程函数。
        tool_name: 工具名称（用于日志）。

    Returns:
        正确处理嵌套事件循环的同步函数。
    """

    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        try:
            if loop is not None and loop.is_running():
                # 已有事件循环运行时，通过线程池避免嵌套循环问题
                future = _SYNC_TOOL_EXECUTOR.submit(asyncio.run, coro(*args, **kwargs))
                return future.result()
            else:
                return asyncio.run(coro(*args, **kwargs))
        except Exception as e:
            logger.error(f"Error invoking MCP tool '{tool_name}' via sync wrapper: {e}", exc_info=True)
            raise

    return sync_wrapper


async def get_mcp_tools() -> list[BaseTool]:
    """从所有已启用的 MCP 服务器异步加载工具。

    流程：
    1. 从磁盘读取最新配置（确保跨进程一致性）
    2. 构建服务器连接参数
    3. 注入初始 OAuth 头到 HTTP/SSE 服务器
    4. 创建 MultiServerMCPClient 并获取工具列表
    5. 为异步工具生成同步包装器

    Returns:
        从 MCP 服务器加载的 LangChain 工具列表。
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning("langchain-mcp-adapters not installed. Install it to enable MCP tools: pip install langchain-mcp-adapters")
        return []

    # 每次从磁盘读取最新配置，确保 Gateway API（独立进程）的修改立即生效
    extensions_config = ExtensionsConfig.from_file()
    servers_config = build_servers_config(extensions_config)

    if not servers_config:
        logger.info("No enabled MCP servers configured")
        return []

    try:
        logger.info(f"Initializing MCP client with {len(servers_config)} server(s)")

        # 注入初始 OAuth 头到服务器连接（工具发现/会话初始化阶段）
        initial_oauth_headers = await get_initial_oauth_headers(extensions_config)
        for server_name, auth_header in initial_oauth_headers.items():
            if server_name not in servers_config:
                continue
            if servers_config[server_name].get("transport") in ("sse", "http"):
                existing_headers = dict(servers_config[server_name].get("headers", {}))
                existing_headers["Authorization"] = auth_header
                servers_config[server_name]["headers"] = existing_headers

        # 构建 OAuth 工具拦截器（每次工具调用时刷新 token）
        tool_interceptors = []
        oauth_interceptor = build_oauth_tool_interceptor(extensions_config)
        if oauth_interceptor is not None:
            tool_interceptors.append(oauth_interceptor)

        client = MultiServerMCPClient(servers_config, tool_interceptors=tool_interceptors, tool_name_prefix=True)

        tools = await client.get_tools()
        logger.info(f"Successfully loaded {len(tools)} tool(s) from MCP servers")

        # 为异步工具生成同步包装器，支持 DeerFlowClient 的同步流式调用
        for tool in tools:
            if getattr(tool, "func", None) is None and getattr(tool, "coroutine", None) is not None:
                tool.func = _make_sync_tool_wrapper(tool.coroutine, tool.name)

        return tools

    except Exception as e:
        logger.error(f"Failed to load MCP tools: {e}", exc_info=True)
        return []
