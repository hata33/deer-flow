"""MCP 工具缓存管理。

通过 mtime（文件修改时间）检测配置变更，实现：
- 启动时初始化（initialize_mcp_tools）
- 运行时懒加载（get_cached_mcp_tools）
- 配置变更时自动失效并重新加载

确保 Gateway API（独立进程）的配置修改能立即反映到 LangGraph Server。
"""

import asyncio
import logging
import os

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

# 全局缓存状态
_mcp_tools_cache: list[BaseTool] | None = None
_cache_initialized = False
_initialization_lock = asyncio.Lock()  # 防止并发初始化
_config_mtime: float | None = None  # 配置文件的修改时间戳


def _get_config_mtime() -> float | None:
    """获取扩展配置文件的修改时间。

    Returns:
        文件修改时间戳，文件不存在时返回 None。
    """
    from deerflow.config.extensions_config import ExtensionsConfig

    config_path = ExtensionsConfig.resolve_config_path()
    if config_path and config_path.exists():
        return os.path.getmtime(config_path)
    return None


def _is_cache_stale() -> bool:
    """检查缓存是否因配置文件变更而过期。

    比较当前配置文件 mtime 与缓存记录的 mtime，
    若文件已被修改则缓存过期。

    Returns:
        缓存是否过期。
    """
    global _config_mtime

    if not _cache_initialized:
        return False  # 未初始化，不算过期

    current_mtime = _get_config_mtime()

    if _config_mtime is None or current_mtime is None:
        return False

    if current_mtime > _config_mtime:
        logger.info(f"MCP config file has been modified (mtime: {_config_mtime} -> {current_mtime}), cache is stale")
        return True

    return False


async def initialize_mcp_tools() -> list[BaseTool]:
    """初始化并缓存 MCP 工具，应在应用启动时调用一次。

    使用 asyncio.Lock 防止并发初始化，确保只执行一次。

    Returns:
        从所有已启用 MCP 服务器加载的工具列表。
    """
    global _mcp_tools_cache, _cache_initialized, _config_mtime

    async with _initialization_lock:
        if _cache_initialized:
            logger.info("MCP tools already initialized")
            return _mcp_tools_cache or []

        from deerflow.mcp.tools import get_mcp_tools

        logger.info("Initializing MCP tools...")
        _mcp_tools_cache = await get_mcp_tools()
        _cache_initialized = True
        _config_mtime = _get_config_mtime()  # 记录配置文件 mtime
        logger.info(f"MCP tools initialized: {len(_mcp_tools_cache)} tool(s) loaded (config mtime: {_config_mtime})")

        return _mcp_tools_cache


def get_cached_mcp_tools() -> list[BaseTool]:
    """获取缓存的 MCP 工具，支持懒初始化和配置变更自动重载。

    若工具未初始化，自动在当前或新事件循环中执行初始化。
    若配置文件已被修改（mtime 检测），自动重置并重新初始化。

    Returns:
        缓存的 MCP 工具列表。
    """
    global _cache_initialized

    # 检查配置文件是否已变更
    if _is_cache_stale():
        logger.info("MCP cache is stale, resetting for re-initialization...")
        reset_mcp_tools_cache()

    if not _cache_initialized:
        logger.info("MCP tools not initialized, performing lazy initialization...")
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 已有事件循环运行时（如 LangGraph Studio），在新线程中初始化
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, initialize_mcp_tools())
                    future.result()
            else:
                loop.run_until_complete(initialize_mcp_tools())
        except RuntimeError:
            # 无事件循环，创建新的
            asyncio.run(initialize_mcp_tools())
        except Exception as e:
            logger.error(f"Failed to lazy-initialize MCP tools: {e}")
            return []

    return _mcp_tools_cache or []


def reset_mcp_tools_cache() -> None:
    """重置 MCP 工具缓存，用于测试或强制重新加载。"""
    global _mcp_tools_cache, _cache_initialized, _config_mtime
    _mcp_tools_cache = None
    _cache_initialized = False
    _config_mtime = None
    logger.info("MCP tools cache reset")
