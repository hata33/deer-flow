"""MCP 工具缓存管理 —— 懒加载、mtime 热更新检测与多事件循环兼容。

本模块管理 MCP 工具的全局缓存，解决以下核心问题：

  1. 避免重复加载：MCP 工具初始化涉及启动外部进程（stdio）或建立网络连接
     （SSE/HTTP），成本高。首次加载后缓存结果，后续调用直接返回缓存。

  2. 配置热更新：Gateway API 修改 extensions_config.json 后运行在独立进程中，
     LangGraph Server 无法直接感知变更。通过 mtime 比对检测文件变化，
     自动失效缓存并触发重新加载。

  3. 多事件循环兼容：MCP 工具可能在 FastAPI 事件循环中初始化，
     也可能在 LangGraph Studio 的不同事件循环中首次访问。
     懒加载逻辑处理了"事件循环已运行"、"无事件循环"、"事件循环未运行"
     三种情况。

缓存失效策略:
  - 基于 extensions_config.json 文件的 mtime（修改时间戳）比对
  - 如果文件的 mtime 大于缓存时记录的 mtime，认为缓存过期
  - 缓存失效后重置为未初始化状态，下次访问时自动重新加载

线程安全:
  - 使用 asyncio.Lock 保护初始化过程，防止并发初始化
  - 全局状态使用模块级变量，整个进程共享
"""

import asyncio
import logging
import os

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

# ── 全局缓存状态 ──────────────────────────────────────────────
# 整个进程共享同一份 MCP 工具缓存

_mcp_tools_cache: list[BaseTool] | None = None  # 缓存的工具列表，None 表示未初始化
_cache_initialized = False  # 是否已完成首次初始化
_initialization_lock = asyncio.Lock()  # 初始化锁，防止并发初始化
_config_mtime: float | None = None  # 配置文件修改时间戳，用于热更新检测


def _get_config_mtime() -> float | None:
    """获取 extensions_config.json 文件的修改时间戳。

    通过 ExtensionsConfig.resolve_config_path() 定位配置文件路径，
    然后使用 os.path.getmtime() 获取修改时间。

    Returns:
        文件的修改时间戳（浮点数秒），文件不存在则返回 None。
    """
    from deerflow.config.extensions_config import ExtensionsConfig

    config_path = ExtensionsConfig.resolve_config_path()
    if config_path and config_path.exists():
        return os.path.getmtime(config_path)
    return None


def _is_cache_stale() -> bool:
    """检测缓存是否因配置文件变更而过期。

    比对当前配置文件的 mtime 与缓存时记录的 mtime：
      - 如果文件被修改过（当前 mtime > 缓存 mtime），缓存过期
      - 如果文件不存在或无法获取 mtime，不认为过期（保守策略）
      - 如果缓存尚未初始化，也不认为过期（由初始化逻辑处理）

    Returns:
        True 表示缓存过期应重新加载，False 表示缓存有效。
    """
    global _config_mtime

    if not _cache_initialized:
        return False  # 尚未初始化，不算过期

    current_mtime = _get_config_mtime()

    # 无法获取 mtime（配置文件可能不存在），保守地认为不过期
    if _config_mtime is None or current_mtime is None:
        return False

    # 文件修改时间晚于缓存时间 → 过期
    if current_mtime > _config_mtime:
        logger.info(f"MCP config file has been modified (mtime: {_config_mtime} -> {current_mtime}), cache is stale")
        return True

    return False


async def initialize_mcp_tools() -> list[BaseTool]:
    """初始化并缓存 MCP 工具列表。

    在应用启动时调用一次。使用 asyncio.Lock 防止并发初始化。
    初始化完成后记录配置文件的 mtime，用于后续热更新检测。

    如果已经初始化过，直接返回缓存结果。

    Returns:
        从所有已启用的 MCP 服务器加载的 LangChain 工具列表。
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
        _config_mtime = _get_config_mtime()  # 记录配置文件 mtime，用于热更新检测
        logger.info(f"MCP tools initialized: {len(_mcp_tools_cache)} tool(s) loaded (config mtime: {_config_mtime})")

        return _mcp_tools_cache


def get_cached_mcp_tools() -> list[BaseTool]:
    """获取缓存的 MCP 工具（懒加载 + 热更新检测）。

    这是获取 MCP 工具的主要入口点。核心设计：

    1. 热更新检测：每次调用都检查配置文件 mtime 是否变化。
       如果变化，重置缓存为未初始化状态。

    2. 懒加载：如果缓存未初始化，自动触发初始化。
       支持多种事件循环环境：
         - 事件循环已运行（如 LangGraph Studio）：
           在独立线程中创建新的事件循环来运行初始化
         - 事件循环存在但未运行：
           直接用当前循环运行
         - 无事件循环：
           创建新的事件循环运行

    为什么需要这种复杂的懒加载逻辑：
      FastAPI 和 LangGraph Studio 运行在不同的上下文中。
      FastAPI 启动时可以通过 lifespan 事件初始化，
      但 LangGraph Studio 可能独立运行，首次访问工具时
      才需要初始化。

    Returns:
        缓存的 MCP 工具列表。初始化失败时返回空列表（不抛异常）。
    """
    global _cache_initialized

    # 步骤 1：检测配置文件是否变更
    if _is_cache_stale():
        logger.info("MCP cache is stale, resetting for re-initialization...")
        reset_mcp_tools_cache()

    # 步骤 2：如果缓存未初始化，执行懒加载
    if not _cache_initialized:
        logger.info("MCP tools not initialized, performing lazy initialization...")
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 事件循环已运行（如 LangGraph Studio 上下文）
                # 不能在已运行的循环中调用 run_until_complete，
                # 需要在独立线程中创建新的事件循环
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, initialize_mcp_tools())
                    future.result()
            else:
                # 事件循环存在但未运行，直接使用
                loop.run_until_complete(initialize_mcp_tools())
        except RuntimeError:
            # 没有事件循环存在，创建一个新的
            try:
                asyncio.run(initialize_mcp_tools())
            except Exception:
                logger.exception("Failed to lazy-initialize MCP tools")
                return []
        except Exception:
            logger.exception("Failed to lazy-initialize MCP tools")
            return []

    return _mcp_tools_cache or []


def reset_mcp_tools_cache() -> None:
    """重置 MCP 工具缓存。

    将缓存状态恢复为"未初始化"，下次调用 get_cached_mcp_tools() 时
    会重新加载工具。

    使用场景：
      - 配置文件变更后主动失效缓存
      - 测试中需要重新加载工具
      - 手动触发 MCP 工具刷新
    """
    global _mcp_tools_cache, _cache_initialized, _config_mtime
    _mcp_tools_cache = None
    _cache_initialized = False
    _config_mtime = None
    logger.info("MCP tools cache reset")
