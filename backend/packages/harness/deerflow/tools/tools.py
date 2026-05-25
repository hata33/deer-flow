"""工具装配管线（Tool Assembly Pipeline）

本模块实现了 DeerFlow 的核心工具装配逻辑，通过 `get_available_tools()` 函数
按优先级顺序收集、过滤、去重所有可用工具。

工具装配管线（按优先级从高到低）：
--------------------------------
1. **配置工具（Config Tools）**
   - 从 config.yaml 的 `tools` 字段加载
   - 支持按 `groups` 过滤
   - 通过 `resolve_variable` 将 `use` 字段解析为实际的 Tool 实例
   - 自动检测并过滤 host-bash 工具（当使用 LocalSandboxProvider 时）

2. **内置工具（Builtin Tools）**
   - present_files：让输出文件对用户可见
   - ask_clarification：向用户请求澄清
   - view_image：读取图片（仅当模型支持视觉功能时）
   - skill_manage：自定义技能管理（仅当 skill_evolution 启用时）
   - tool_search：延迟加载工具搜索（仅当 MCP 工具存在且 tool_search 启用时）

3. **MCP 工具（MCP Tools）**
   - 从 MCP 服务器的缓存中获取
   - 启动时通过 `initialize_mcp_tools()` 预初始化
   - 支持 tool_search 延迟注册机制

4. **子代理工具（Subagent Tools）**
   - task：任务委派（仅当 subagent_enabled=True 时）
   - 防止递归嵌套：子代理内部不会再次加载子代理工具

5. **ACP 工具（ACP Tools）**
   - invoke_acp_agent：调用 ACP 兼容的外部代理
   - 仅当配置了 ACP 代理时才注册

去重策略：
--------
所有工具按上述顺序合并后，按工具名去重。高优先级的工具优先保留，
重复名称的工具会被跳过并记录警告日志。

注意事项：
--------
- MCP 工具的配置始终从磁盘重新读取（ExtensionsConfig.from_file()），
  以确保 Gateway API 所做的配置更改能立即生效。
- 当 tool_search 启用时，MCP 工具会被注册到 DeferredToolRegistry，
  代理需要通过 tool_search 工具按需发现和加载。
"""

import logging

from langchain.tools import BaseTool

from deerflow.config import get_app_config
from deerflow.config.app_config import AppConfig
from deerflow.reflection import resolve_variable
from deerflow.sandbox.security import is_host_bash_allowed
from deerflow.tools.builtins import ask_clarification_tool, present_file_tool, task_tool, view_image_tool
from deerflow.tools.builtins.tool_search import get_deferred_registry
from deerflow.tools.sync import make_sync_tool_wrapper

logger = logging.getLogger(__name__)

# 始终包含的内置工具列表
BUILTIN_TOOLS = [
    present_file_tool,
    ask_clarification_tool,
]

# 子代理工具列表（仅在 subagent_enabled=True 时包含）
SUBAGENT_TOOLS = [
    task_tool,
    # task_status_tool 不再暴露给 LLM（后端内部处理轮询）
]


def _is_host_bash_tool(tool: object) -> bool:
    """判断工具配置是否代表 host-bash 执行面。

    当 LocalSandboxProvider 处于活跃状态时，host-bash 工具不应暴露给 LLM，
    因为它可能绕过沙箱安全限制。

    检测规则：
    - group == "bash" → 视为 host-bash 工具
    - use == "deerflow.sandbox.tools:bash_tool" → 视为 host-bash 工具
    """
    group = getattr(tool, "group", None)
    use = getattr(tool, "use", None)
    if group == "bash":
        return True
    if use == "deerflow.sandbox.tools:bash_tool":
        return True
    return False


def _ensure_sync_invocable_tool(tool: BaseTool) -> BaseTool:
    """为纯异步工具附加同步包装器。

    某些代理调用路径（如嵌入式 DeerFlowClient）运行在同步上下文中，
    需要通过 `tool.func` 同步调用工具。如果工具只定义了 `coroutine`
    而没有 `func`，则使用 `make_sync_tool_wrapper` 自动生成同步包装器。
    """
    if getattr(tool, "func", None) is None and getattr(tool, "coroutine", None) is not None:
        tool.func = make_sync_tool_wrapper(tool.coroutine, tool.name)
    return tool


def get_available_tools(
    groups: list[str] | None = None,
    include_mcp: bool = True,
    model_name: str | None = None,
    subagent_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
) -> list[BaseTool]:
    """按优先级顺序获取所有可用工具。

    工具装配管线：
    1. 从配置文件加载工具（config.yaml → resolve_variable → Tool 实例）
    2. 过滤 host-bash 工具（当 LocalSandboxProvider 活跃时）
    3. 检测配置名称与工具 .name 属性的不匹配并发出警告
    4. 条件性添加内置工具（skill_manage、view_image、subagent tools）
    5. 从缓存加载 MCP 工具（如果启用）
    6. 如果 tool_search 启用，将 MCP 工具注册到延迟注册表
    7. 加载 ACP 代理工具（如果配置了 ACP 代理）
    8. 合并去重：config tools > builtins > MCP > ACP

    注意：MCP 工具应在应用启动时通过 `deerflow.mcp` 模块的
    `initialize_mcp_tools()` 进行初始化。

    Args:
        groups: 可选的工具组过滤列表。如果为 None，则包含所有组的工具。
        include_mcp: 是否包含 MCP 服务器提供的工具（默认为 True）。
        model_name: 可选的模型名称，用于判断是否应包含视觉工具。
        subagent_enabled: 是否包含子代理工具（task, task_status）。

    Returns:
        去重后的可用工具列表。
    """
    # ── 第一步：从配置文件加载工具 ──
    config = app_config or get_app_config()
    tool_configs = [tool for tool in config.tools if groups is None or tool.group in groups]

    # 当 LocalSandboxProvider 活跃时，不暴露 host-bash 工具
    if not is_host_bash_allowed(config):
        tool_configs = [tool for tool in tool_configs if not _is_host_bash_tool(tool)]

    # 通过 resolve_variable 将配置中的 use 字符串解析为实际的 Tool 实例
    loaded_tools_raw = [(cfg, resolve_variable(cfg.use, BaseTool)) for cfg in tool_configs]

    # 检测配置名称与工具 .name 属性的不匹配（issue #1803 的根本原因）
    # 这种不匹配会导致 LLM 在工具 schema 中看到一个名称，但运行时路由器
    # 识别另一个名称，产生"不是有效工具"的错误。
    for cfg, loaded in loaded_tools_raw:
        if cfg.name != loaded.name:
            logger.warning(
                "Tool name mismatch: config name %r does not match tool .name %r (use: %s). The tool's own .name will be used for binding.",
                cfg.name,
                loaded.name,
                cfg.use,
            )

    # 为纯异步工具附加同步包装器（嵌入式客户端需要）
    loaded_tools = [_ensure_sync_invocable_tool(t) for _, t in loaded_tools_raw]

    # ── 第二步：条件性添加内置工具 ──
    builtin_tools = BUILTIN_TOOLS.copy()

    # 如果启用了技能进化功能，添加 skill_manage 工具
    skill_evolution_config = getattr(config, "skill_evolution", None)
    if getattr(skill_evolution_config, "enabled", False):
        from deerflow.tools.skill_manage_tool import skill_manage_tool

        builtin_tools.append(skill_manage_tool)

    # 仅在运行时参数启用时添加子代理工具
    if subagent_enabled:
        builtin_tools.extend(SUBAGENT_TOOLS)
        logger.info("Including subagent tools (task)")

    # 如果未指定模型名称，使用第一个模型作为默认值
    if model_name is None and config.models:
        model_name = config.models[0].name

    # 仅当模型支持视觉功能时添加 view_image 工具
    model_config = config.get_model_config(model_name) if model_name else None
    if model_config is not None and model_config.supports_vision:
        builtin_tools.append(view_image_tool)
        logger.info(f"Including view_image_tool for model '{model_name}' (supports_vision=True)")

    # ── 第三步：加载 MCP 工具（从缓存） ──
    # 注意：使用 ExtensionsConfig.from_file() 而非 config.extensions，
    # 以始终从磁盘读取最新配置。这确保通过 Gateway API（在独立进程中运行）
    # 所做的更改在加载 MCP 工具时能立即生效。
    mcp_tools = []
    if include_mcp:
        try:
            from deerflow.config.extensions_config import ExtensionsConfig
            from deerflow.mcp.cache import get_cached_mcp_tools

            extensions_config = ExtensionsConfig.from_file()
            if extensions_config.get_enabled_mcp_servers():
                mcp_tools = get_cached_mcp_tools()
                if mcp_tools:
                    logger.info(f"Using {len(mcp_tools)} cached MCP tool(s)")

                    # 当 tool_search 启用时，将 MCP 工具注册到延迟注册表，
                    # 并将 tool_search 添加到内置工具列表。
                    if config.tool_search.enabled:
                        from deerflow.tools.builtins.tool_search import DeferredToolRegistry, set_deferred_registry
                        from deerflow.tools.builtins.tool_search import tool_search as tool_search_tool

                        # 如果当前异步上下文已存在注册表，则复用它。
                        # `get_available_tools` 在子代理生成时会被重入调用
                        #（`task_tool` 调用它来构建子代理的工具集），之前我们
                        # 无条件重建注册表——这会清除父代理的 tool_search 提升。
                        # DeferredToolFilterMiddleware 随后在后续模型调用中
                        # 重新隐藏这些工具，导致代理能看到工具名但无法调用
                        #（issue #2884）。contextvars 已经提供了我们需要的
                        # 生命周期语义：新的请求/图运行在新的 asyncio 任务中
                        # 启动，ContextVar 为默认值 None，因此复用仅在
                        # 单次运行内的重入调用中触发。
                        #
                        # 故意不对当前 mcp_tools 快照进行协调。MCP 缓存仅在
                        # extensions_config.json 的 mtime 变化时刷新，实际上
                        # 这发生在图运行之间——而非运行内。即使刷新在运行中间
                        # 发生，已构建的主代理 ToolNode 仍持有之前的工具集
                        #（LangGraph 在图构建时绑定工具），因此全新的 MCP 工具
                        # 实际上也无法被调用。DeferredToolRegistry 不保留已提升
                        # 工具的名称（promote() 会完全删除条目），因此将注册表
                        # 与新的 mcp_tools 列表重新同步会错误地将已提升的工具
                        # 分类为新工具并重新注册为延迟——这正是此修复要防止的 bug。
                        existing_registry = get_deferred_registry()
                        if existing_registry is None:
                            # 首次创建注册表：将所有 MCP 工具注册为延迟工具
                            registry = DeferredToolRegistry()
                            for t in mcp_tools:
                                registry.register(t)
                            set_deferred_registry(registry)
                            logger.info(f"Tool search active: {len(mcp_tools)} tools deferred")
                        else:
                            # 复用现有注册表：保留已提升的工具状态
                            mcp_tool_names = {t.name for t in mcp_tools}
                            still_deferred = len(existing_registry)
                            promoted_count = max(0, len(mcp_tool_names) - still_deferred)
                            logger.info(f"Tool search active (preserved promotions): {still_deferred} tools deferred, {promoted_count} already promoted")
                        builtin_tools.append(tool_search_tool)
        except ImportError:
            logger.warning("MCP module not available. Install 'langchain-mcp-adapters' package to enable MCP tools.")
        except Exception as e:
            logger.error(f"Failed to get cached MCP tools: {e}")

    # ── 第四步：加载 ACP 代理工具 ──
    acp_tools: list[BaseTool] = []
    try:
        from deerflow.tools.builtins.invoke_acp_agent_tool import build_invoke_acp_agent_tool

        if app_config is None:
            from deerflow.config.acp_config import get_acp_agents

            acp_agents = get_acp_agents()
        else:
            acp_agents = getattr(config, "acp_agents", {}) or {}
        if acp_agents:
            acp_tools.append(build_invoke_acp_agent_tool(acp_agents))
            logger.info(f"Including invoke_acp_agent tool ({len(acp_agents)} agent(s): {list(acp_agents.keys())})")
    except Exception as e:
        logger.warning(f"Failed to load ACP tool: {e}")

    logger.info(f"Total tools loaded: {len(loaded_tools)}, built-in tools: {len(builtin_tools)}, MCP tools: {len(mcp_tools)}, ACP tools: {len(acp_tools)}")

    # ── 第五步：合并去重 ──
    # 按优先级去重：config 工具 > 内置工具 > MCP 工具 > ACP 工具。
    # 重复的工具名称会导致 LLM 收到模糊或拼接的函数 schema（issue #1803）。
    all_tools = [_ensure_sync_invocable_tool(t) for t in loaded_tools + builtin_tools + mcp_tools + acp_tools]
    seen_names: set[str] = set()
    unique_tools: list[BaseTool] = []
    for t in all_tools:
        if t.name not in seen_names:
            unique_tools.append(t)
            seen_names.add(t.name)
        else:
            logger.warning(
                "Duplicate tool name %r detected and skipped — check your config.yaml and MCP server registrations (issue #1803).",
                t.name,
            )
    return unique_tools
