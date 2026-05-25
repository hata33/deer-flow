"""MCP 工具加载入口 —— 整合配置、客户端、OAuth 和拦截器。

本模块是 MCP 工具加载的核心入口，职责是：
  1. 从 extensions_config.json 读取最新配置（直接从磁盘，不用缓存）
  2. 构建服务器连接参数（调用 client.build_servers_config）
  3. 获取 OAuth 初始认证头（连接建立阶段需要）
  4. 构建工具拦截器链（OAuth + 自定义拦截器）
  5. 通过 langchain-mcp-adapters 的 MultiServerMCPClient 加载工具
  6. 将异步工具包装为同步可调用形式

为什么每次都从磁盘读取配置:
  Gateway API 修改 extensions_config.json 后运行在独立进程中，
  如果使用内存缓存的配置，会错过其他进程的修改。
  直接从磁盘读取确保初始化时使用的是最新配置。

拦截器体系:
  langchain-mcp-adapters 支持工具拦截器（tool interceptor），
  在每次 MCP 工具调用前后执行自定义逻辑。
  本模块构建的拦截器链包括：
    1. OAuth 拦截器（自动注入 Authorization 头）
    2. 自定义拦截器（通过 extensions_config.json 的 mcpInterceptors 字段声明）

同步包装:
  DeerFlow 的嵌入式客户端（DeerFlowClient）在同步上下文中使用工具，
  但 MCP 工具本质上是异步的（通过 stdio/SSE/HTTP 通信）。
  make_sync_tool_wrapper 将异步工具包装为同步可调用形式，
  在内部使用线程池处理异步调用。
"""

import logging

from langchain_core.tools import BaseTool

from deerflow.config.extensions_config import ExtensionsConfig
from deerflow.mcp.client import build_servers_config
from deerflow.mcp.oauth import build_oauth_tool_interceptor, get_initial_oauth_headers
from deerflow.reflection import resolve_variable
from deerflow.tools.sync import make_sync_tool_wrapper

logger = logging.getLogger(__name__)


async def get_mcp_tools() -> list[BaseTool]:
    """从所有已启用的 MCP 服务器加载工具。

    这是 MCP 工具加载的核心函数，被 cache.py 的初始化逻辑调用。

    执行流程:
      1. 检查 langchain-mcp-adapters 是否已安装
      2. 从磁盘读取最新的 extensions_config.json
      3. 构建所有已启用服务器的连接参数
      4. 获取 OAuth 初始认证头并注入到 SSE/HTTP 服务器配置中
      5. 构建拦截器链（OAuth + 自定义拦截器）
      6. 创建 MultiServerMCPClient 并获取所有工具
      7. 为异步工具添加同步包装器

    错误处理:
      - langchain-mcp-adapters 未安装 → 返回空列表 + 警告日志
      - 没有已启用的服务器 → 返回空列表
      - 单个服务器配置失败 → 不影响其他服务器
      - 整体加载失败 → 返回空列表 + 错误日志

    Returns:
        从所有已启用的 MCP 服务器加载的 LangChain 工具列表。
        加载失败时返回空列表（不抛异常）。
    """
    # ── 步骤 1: 检查依赖 ─────────────────────────────────────
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning("langchain-mcp-adapters not installed. Install it to enable MCP tools: pip install langchain-mcp-adapters")
        return []

    # ── 步骤 2: 从磁盘读取最新配置 ───────────────────────────
    # 注意：使用 from_file() 而非 get_extensions_config()，
    # 确保读取的是磁盘上的最新内容，而非内存缓存。
    # 这样可以捕获 Gateway API（独立进程）对配置的修改。
    extensions_config = ExtensionsConfig.from_file()
    servers_config = build_servers_config(extensions_config)

    if not servers_config:
        logger.info("No enabled MCP servers configured")
        return []

    try:
        logger.info(f"Initializing MCP client with {len(servers_config)} server(s)")

        # ── 步骤 3: 注入 OAuth 初始认证头 ─────────────────────
        # 在 SSE/HTTP 连接建立前获取 OAuth 令牌，
        # 确保工具发现（tool discovery）阶段就能通过认证。
        initial_oauth_headers = await get_initial_oauth_headers(extensions_config)
        for server_name, auth_header in initial_oauth_headers.items():
            if server_name not in servers_config:
                continue
            # 只对 SSE/HTTP 传输类型注入（stdio 不需要）
            if servers_config[server_name].get("transport") in ("sse", "http"):
                existing_headers = dict(servers_config[server_name].get("headers", {}))
                existing_headers["Authorization"] = auth_header
                servers_config[server_name]["headers"] = existing_headers

        # ── 步骤 4: 构建拦截器链 ─────────────────────────────
        tool_interceptors = []

        # 4a. OAuth 拦截器：在每次工具调用时自动注入认证头
        oauth_interceptor = build_oauth_tool_interceptor(extensions_config)
        if oauth_interceptor is not None:
            tool_interceptors.append(oauth_interceptor)

        # 4b. 自定义拦截器：通过 extensions_config.json 的 mcpInterceptors 字段声明
        # 格式: "mcpInterceptors": ["pkg.module:builder_func", ...]
        # 每个 builder_func 被调用后应返回一个 async (request, handler) -> response 函数
        raw_interceptor_paths = (extensions_config.model_extra or {}).get("mcpInterceptors")
        if isinstance(raw_interceptor_paths, str):
            # 支持单个字符串（自动包装为列表）
            raw_interceptor_paths = [raw_interceptor_paths]
        elif not isinstance(raw_interceptor_paths, list):
            if raw_interceptor_paths is not None:
                logger.warning(f"mcpInterceptors must be a list of strings, got {type(raw_interceptor_paths).__name__}; skipping")
            raw_interceptor_paths = []

        for interceptor_path in raw_interceptor_paths:
            try:
                # 使用 reflection 系统解析拦截器构建函数
                builder = resolve_variable(interceptor_path)
                interceptor = builder()
                if callable(interceptor):
                    tool_interceptors.append(interceptor)
                    logger.info(f"Loaded MCP interceptor: {interceptor_path}")
                elif interceptor is not None:
                    logger.warning(f"Builder {interceptor_path} returned non-callable {type(interceptor).__name__}; skipping")
            except Exception as e:
                # 单个拦截器加载失败不影响其他拦截器
                logger.warning(f"Failed to load MCP interceptor {interceptor_path}: {e}", exc_info=True)

        # ── 步骤 5: 创建 MCP 客户端并加载工具 ─────────────────
        # tool_name_prefix=True: 工具名会添加服务器名前缀（如 "filesystem__read_file"）
        # 避免不同服务器的同名工具冲突
        client = MultiServerMCPClient(servers_config, tool_interceptors=tool_interceptors, tool_name_prefix=True)

        # 获取所有服务器提供的工具
        tools = await client.get_tools()
        logger.info(f"Successfully loaded {len(tools)} tool(s) from MCP servers")

        # ── 步骤 6: 同步包装 ─────────────────────────────────
        # DeerFlow 的嵌入式客户端（DeerFlowClient）在同步上下文中使用工具，
        # 但 MCP 工具只有 coroutine（异步实现）没有 func（同步实现）。
        # 为这些工具添加同步包装器，使其可以在同步上下文中调用。
        for tool in tools:
            if getattr(tool, "func", None) is None and getattr(tool, "coroutine", None) is not None:
                tool.func = make_sync_tool_wrapper(tool.coroutine, tool.name)

        return tools

    except Exception as e:
        # 整体加载失败：记录错误但不抛异常，返回空列表
        # 这样即使 MCP 完全不可用，Agent 的其他功能（内置工具、沙箱等）仍能正常工作
        logger.error(f"Failed to load MCP tools: {e}", exc_info=True)
        return []
