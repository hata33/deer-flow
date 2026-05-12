"""
工具加载与组装模块。

负责根据应用配置、模型能力、运行时参数等条件，组装 agent 可用的完整工具列表。
工具来源包括：配置文件定义的工具、内置工具、MCP 工具、ACP 工具，以及按需加载的子代理工具和视觉工具。
"""

import logging

from langchain.tools import BaseTool

from deerflow.config import get_app_config
from deerflow.reflection import resolve_variable
from deerflow.sandbox.security import is_host_bash_allowed
from deerflow.tools.builtins import ask_clarification_tool, present_file_tool, task_tool, view_image_tool
from deerflow.tools.builtins.tool_search import reset_deferred_registry

logger = logging.getLogger(__name__)

# 核心 内置工具：所有 agent 默认可用
BUILTIN_TOOLS = [
    present_file_tool,       # 向用户展示输出文件（仅 /mnt/user-data/outputs）
    ask_clarification_tool,  # 请求用户澄清（被 ClarificationMiddleware 拦截并中断流程）
]

# 子代理专用工具：仅在 subagent_enabled 时注入
SUBAGENT_TOOLS = [
    task_tool,
    # task_status_tool is no longer exposed to LLM (backend handles polling internally)
]


def _is_host_bash_tool(tool: object) -> bool:
    """判断工具配置是否代表宿主机 bash 执行环境。"""
    group = getattr(tool, "group", None)
    use = getattr(tool, "use", None)
    if group == "bash":
        return True
    if use == "deerflow.sandbox.tools:bash_tool":
        return True
    return False


def get_available_tools(
    groups: list[str] | None = None,
    include_mcp: bool = True,
    model_name: str | None = None,
    subagent_enabled: bool = False,
) -> list[BaseTool]:
    """获取 agent 可用的完整工具列表。

    注意：MCP 工具应在应用启动时通过 ``deerflow.mcp`` 模块的
    ``initialize_mcp_tools()`` 完成初始化。

    Args:
        groups: 工具分组过滤列表，为 None 时加载全部。
        include_mcp: 是否包含 MCP 服务器提供的工具。
        model_name: 模型名称，用于判断是否应加载视觉工具。
        subagent_enabled: 是否包含子代理委派工具（task、task_status）。

    Returns:
        可用工具列表。
    """
    config = get_app_config()

    # 第一步：从 config.yaml 中按 groups 过滤工具配置
    tool_configs = [tool for tool in config.tools if groups is None or tool.group in groups]

    # 当 LocalSandboxProvider 生效时，默认不暴露宿主机 bash 工具以保障安全
    if not is_host_bash_allowed(config):
        tool_configs = [tool for tool in tool_configs if not _is_host_bash_tool(tool)]

    # 通过反射机制动态加载工具实例（resolve_variable 将 "module.path:var" 解析为对象）
    loaded_tools = [resolve_variable(tool.use, BaseTool) for tool in tool_configs]

    # 第二步：组装内置工具
    builtin_tools = BUILTIN_TOOLS.copy()

    # 仅在运行时参数启用时注入子代理工具
    if subagent_enabled:
        builtin_tools.extend(SUBAGENT_TOOLS)
        logger.info("Including subagent tools (task)")

    # 未指定模型名时，使用配置中的第一个模型作为默认值
    if model_name is None and config.models:
        model_name = config.models[0].name

    # 仅当模型支持视觉能力时才加载 view_image 工具
    model_config = config.get_model_config(model_name) if model_name else None
    if model_config is not None and model_config.supports_vision:
        builtin_tools.append(view_image_tool)
        logger.info(f"Including view_image_tool for model '{model_name}' (supports_vision=True)")

    # 第三步：加载 MCP 缓存工具
    # 注意：使用 ExtensionsConfig.from_file() 而非 config.extensions，
    # 以确保每次都从磁盘读取最新配置（Gateway API 运行在独立进程中，
    # 运行时修改能立即反映到 MCP 工具加载中）。
    mcp_tools = []
    # 提前重置延迟注册表，防止前一次调用留下的陈旧状态
    reset_deferred_registry()
    if include_mcp:
        try:
            from deerflow.config.extensions_config import ExtensionsConfig
            from deerflow.mcp.cache import get_cached_mcp_tools

            extensions_config = ExtensionsConfig.from_file()
            if extensions_config.get_enabled_mcp_servers():
                mcp_tools = get_cached_mcp_tools()
                if mcp_tools:
                    logger.info(f"Using {len(mcp_tools)} cached MCP tool(s)")

                    # 当 tool_search 启用时，将 MCP 工具注册到延迟注册表中，
                    # 并将 tool_search 工具加入内置工具列表，实现按需查找。
                    if config.tool_search.enabled:
                        from deerflow.tools.builtins.tool_search import DeferredToolRegistry, set_deferred_registry
                        from deerflow.tools.builtins.tool_search import tool_search as tool_search_tool

                        registry = DeferredToolRegistry()
                        for t in mcp_tools:
                            registry.register(t)
                        set_deferred_registry(registry)
                        builtin_tools.append(tool_search_tool)
                        logger.info(f"Tool search active: {len(mcp_tools)} tools deferred")
        except ImportError:
            logger.warning("MCP module not available. Install 'langchain-mcp-adapters' package to enable MCP tools.")
        except Exception as e:
            logger.error(f"Failed to get cached MCP tools: {e}")

    # 第四步：加载 ACP（Agent Communication Protocol）代理工具
    acp_tools: list[BaseTool] = []
    try:
        from deerflow.config.acp_config import get_acp_agents
        from deerflow.tools.builtins.invoke_acp_agent_tool import build_invoke_acp_agent_tool

        acp_agents = get_acp_agents()
        if acp_agents:
            acp_tools.append(build_invoke_acp_agent_tool(acp_agents))
            logger.info(f"Including invoke_acp_agent tool ({len(acp_agents)} agent(s): {list(acp_agents.keys())})")
    except Exception as e:
        logger.warning(f"Failed to load ACP tool: {e}")

    # 汇总日志并返回完整工具列表
    logger.info(f"Total tools loaded: {len(loaded_tools)}, built-in tools: {len(builtin_tools)}, MCP tools: {len(mcp_tools)}, ACP tools: {len(acp_tools)}")
    return loaded_tools + builtin_tools + mcp_tools + acp_tools
