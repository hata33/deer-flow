"""ACP 代理调用工具（Invoke ACP Agent Tool）

本模块实现了 `invoke_acp_agent` 工具，用于调用 ACP（Agent Client Protocol）
兼容的外部代理。

ACP 协议说明：
------------
ACP（Agent Client Protocol）是一种标准化的代理通信协议，允许不同的代理
实现之间进行互操作。DeerFlow 作为 ACP 客户端，可以调用任何 ACP 兼容的外部
代理（如 Codex）并获取其响应。

工具构建：
--------
`build_invoke_acp_agent_tool()` 是一个工厂函数，根据配置的 ACP 代理列表
动态构建工具实例。工具描述中包含所有可用代理的列表，以便 LLM 知道可以
调用哪些代理。

每线程工作空间：
------------
每个线程获得独立的工作空间目录：
    {base_dir}/threads/{thread_id}/acp-workspace/

这确保了并发会话之间不会互相读取或覆盖输出。
当 thread_id 不可用时（如嵌入式/直接调用），回退到全局工作空间：
    {base_dir}/acp-workspace/

ACP 调用流程：
------------
1. 启动 ACP 代理进程（spawn_agent_process）
2. 初始化连接（协议版本、客户端能力）
3. 创建会话（工作目录、MCP 服务器配置、模型）
4. 发送提示（prompt）
5. 收集流式文本响应（_CollectingClient）
6. 返回收集到的文本

权限处理：
--------
ACP 代理可能在执行过程中请求权限（如文件写入、命令执行）。
`_build_permission_response()` 处理权限请求：
- **auto_approve=True**：自动选择第一个 allow_once 或 allow_always 选项
- **auto_approve=False**（默认）：始终取消权限请求

MCP 服务器传递：
-------------
调用 ACP 代理时，DeerFlow 会将已启用的 MCP 服务器配置传递给代理，
使代理也能访问 MCP 工具。配置格式从 DeerFlow 的内部格式转换为
ACP 线格式（mcp_servers 列表）。

错误处理：
--------
- 代理不存在：返回错误消息和可用代理列表
- ACP 包未安装：提示运行 uv sync
- 命令未找到：提供可操作的修复建议
- 其他异常：记录详细错误日志并返回用户友好的错误消息
"""

import logging
import os
import shutil
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolArg, StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class _InvokeACPAgentInput(BaseModel):
    """invoke_acp_agent 工具的输入参数模型。"""
    agent: str = Field(description="Name of the ACP agent to invoke")
    prompt: str = Field(description="The concise task prompt to send to the agent")


def _get_work_dir(thread_id: str | None) -> str:
    """获取每线程的 ACP 工作空间目录。

    每个线程获得独立的工作空间：
        {base_dir}/threads/{thread_id}/acp-workspace/

    确保并发会话之间不会互相读取或覆盖 ACP 代理的输出。

    当 thread_id 不可用时（如嵌入式/直接调用），回退到全局工作空间：
        {base_dir}/acp-workspace/

    目录不存在时会自动创建。

    Args:
        thread_id: 当前线程 ID

    Returns:
        工作空间的绝对物理文件系统路径
    """
    from deerflow.config.paths import get_paths
    from deerflow.runtime.user_context import get_effective_user_id

    paths = get_paths()
    if thread_id:
        try:
            work_dir = paths.acp_workspace_dir(thread_id, user_id=get_effective_user_id())
        except ValueError:
            logger.warning("Invalid thread_id %r for ACP workspace, falling back to global", thread_id)
            work_dir = paths.base_dir / "acp-workspace"
    else:
        work_dir = paths.base_dir / "acp-workspace"

    work_dir.mkdir(parents=True, exist_ok=True)
    logger.info("ACP agent work_dir: %s", work_dir)
    return str(work_dir)


def _build_mcp_servers() -> dict[str, dict[str, Any]]:
    """从 DeerFlow 已启用的 MCP 服务器构建 ACP mcpServers 配置。

    返回名称到配置的映射，用于 LangChain MCP 适配器。
    """
    from deerflow.config.extensions_config import ExtensionsConfig
    from deerflow.mcp.client import build_servers_config

    return build_servers_config(ExtensionsConfig.from_file())


def _build_acp_mcp_servers() -> list[dict[str, Any]]:
    """为 new_session 构建 ACP mcpServers 负载。

    ACP 客户端期望服务器对象的列表，而 DeerFlow 的 MCP 辅助函数
    返回名称 -> 配置的映射（用于 LangChain MCP 适配器）。
    此辅助函数将已启用的服务器转换为 ACP 线格式。

    支持的传输类型：
    - stdio：命令行启动（需要 command 字段）
    - http/sse：HTTP/SSE 连接（需要 url 字段）
    """
    from deerflow.config.extensions_config import ExtensionsConfig

    extensions_config = ExtensionsConfig.from_file()
    enabled_servers = extensions_config.get_enabled_mcp_servers()

    mcp_servers: list[dict[str, Any]] = []
    for name, server_config in enabled_servers.items():
        transport_type = server_config.type or "stdio"
        payload: dict[str, Any] = {"name": name, "type": transport_type}

        if transport_type == "stdio":
            if not server_config.command:
                raise ValueError(f"MCP server '{name}' with stdio transport requires 'command' field")
            payload["command"] = server_config.command
            payload["args"] = server_config.args
            payload["env"] = [{"name": key, "value": value} for key, value in server_config.env.items()]
        elif transport_type in ("http", "sse"):
            if not server_config.url:
                raise ValueError(f"MCP server '{name}' with {transport_type} transport requires 'url' field")
            payload["url"] = server_config.url
            payload["headers"] = [{"name": key, "value": value} for key, value in server_config.headers.items()]
        else:
            raise ValueError(f"MCP server '{name}' has unsupported transport type: {transport_type}")

        mcp_servers.append(payload)

    return mcp_servers


def _build_permission_response(options: list[Any], *, auto_approve: bool) -> Any:
    """构建 ACP 权限响应。

    当 auto_approve 为 True 时，选择第一个 allow_once（优先）
    或 allow_always 选项。当为 False（默认）时，始终取消——
    权限请求必须由 ACP 代理自身的策略处理，或者代理必须配置为
    不请求权限即可运行。

    Args:
        options: 可用的权限选项列表
        auto_approve: 是否自动批准权限请求

    Returns:
        ACP RequestPermissionResponse 对象
    """
    from acp import RequestPermissionResponse
    from acp.schema import AllowedOutcome, DeniedOutcome

    if auto_approve:
        for preferred_kind in ("allow_once", "allow_always"):
            for option in options:
                if getattr(option, "kind", None) != preferred_kind:
                    continue

                option_id = getattr(option, "option_id", None)
                if option_id is None:
                    option_id = getattr(option, "optionId", None)
                if option_id is None:
                    continue

                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", optionId=option_id),
                )

    return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))


def _format_invocation_error(agent: str, cmd: str, exc: Exception) -> str:
    """返回带有可操作修复建议的用户友好 ACP 调用错误消息。

    针对 FileNotFoundError 提供特别详细的建议：
    - 如果命令是 codex-acp 且系统安装了 codex，提示安装 ACP 适配器
    - 其他情况提示安装代理二进制文件或更新配置
    """
    if not isinstance(exc, FileNotFoundError):
        return f"Error invoking ACP agent '{agent}': {exc}"

    message = f"Error invoking ACP agent '{agent}': Command '{cmd}' was not found on PATH."
    if cmd == "codex-acp" and shutil.which("codex"):
        return f"{message} The installed `codex` CLI does not speak ACP directly. Install a Codex ACP adapter (for example `npx @zed-industries/codex-acp`) or update `acp_agents.codex.command` and `args` in config.yaml."

    return f"{message} Install the agent binary or update `acp_agents.{agent}.command` in config.yaml."


def build_invoke_acp_agent_tool(agents: dict) -> BaseTool:
    """创建包含可用代理描述的 invoke_acp_agent 工具。

    工具描述中包含可用代理列表，以便 LLM 知道可以调用哪些代理
    而不需要硬编码名称。

    Args:
        agents: 代理名称 -> ACPAgentConfig 的映射

    Returns:
        可直接添加到工具列表的 LangChain BaseTool
    """
    # 构建工具描述中的代理列表
    agent_lines = "\n".join(f"- {name}: {cfg.description}" for name, cfg in agents.items())
    description = (
        "Invoke an external ACP-compatible agent and return its final response.\n\n"
        "Available agents:\n"
        f"{agent_lines}\n\n"
        "IMPORTANT: ACP agents operate in their own independent workspace. "
        "Do NOT include /mnt/user-data paths in the prompt. "
        "Give the agent a self-contained task description — it will produce results in its own workspace. "
        "After the agent completes, its output files are accessible at /mnt/acp-workspace/ (read-only)."
    )

    # 在闭包中捕获 agents 引用
    _agents = dict(agents)

    async def _invoke(agent: str, prompt: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
        """ACP 代理调用的核心实现。

        执行流程：
        1. 验证代理名称
        2. 解析线程 ID 和工作空间目录
        3. 启动 ACP 代理进程
        4. 初始化连接（协议握手）
        5. 创建会话（传递工作目录和 MCP 服务器配置）
        6. 发送提示
        7. 收集并返回响应文本
        """
        logger.info("Invoking ACP agent %s (prompt length: %d)", agent, len(prompt))
        logger.debug("Invoking ACP agent %s with prompt: %.200s%s", agent, prompt, "..." if len(prompt) > 200 else "")
        if agent not in _agents:
            available = ", ".join(_agents.keys())
            return f"Error: Unknown agent '{agent}'. Available: {available}"

        agent_config = _agents[agent]
        thread_id: str | None = ((config or {}).get("configurable") or {}).get("thread_id")

        try:
            from acp import PROTOCOL_VERSION, Client, text_block
            from acp.schema import ClientCapabilities, Implementation
        except ImportError:
            return "Error: agent-client-protocol package is not installed. Run `uv sync` to install project dependencies."

        class _CollectingClient(Client):
            """最小化 ACP 客户端，从会话更新中收集流式文本。"""

            def __init__(self) -> None:
                self._chunks: list[str] = []

            @property
            def collected_text(self) -> str:
                """返回收集到的所有文本块的拼接结果。"""
                return "".join(self._chunks)

            async def session_update(self, session_id: str, update, **kwargs) -> None:  # type: ignore[override]
                """处理 ACP 会话更新事件，收集文本内容块。"""
                try:
                    from acp.schema import TextContentBlock

                    if hasattr(update, "content") and isinstance(update.content, TextContentBlock):
                        self._chunks.append(update.content.text)
                except Exception:
                    pass

            async def request_permission(self, options, session_id: str, tool_call, **kwargs):  # type: ignore[override]
                """处理 ACP 代理的权限请求。

                根据 auto_approve_permissions 配置决定是否自动批准。
                """
                response = _build_permission_response(options, auto_approve=agent_config.auto_approve_permissions)
                outcome = response.outcome.outcome
                if outcome == "selected":
                    logger.info("ACP permission auto-approved for tool call %s in session %s", tool_call.tool_call_id, session_id)
                else:
                    logger.warning("ACP permission denied for tool call %s in session %s (set auto_approve_permissions: true in config.yaml to enable)", tool_call.tool_call_id, session_id)
                return response

        client = _CollectingClient()
        cmd = agent_config.command
        args = agent_config.args or []
        physical_cwd = _get_work_dir(thread_id)

        # 构建 MCP 服务器配置
        try:
            mcp_servers = _build_acp_mcp_servers()
        except ValueError as exc:
            logger.warning(
                "Invalid MCP server configuration for ACP agent '%s'; continuing without MCP servers: %s",
                agent,
                exc,
            )
            mcp_servers = []

        # 处理代理环境变量（支持 $VAR_NAME 格式引用系统环境变量）
        agent_env: dict[str, str] | None = None
        if agent_config.env:
            agent_env = {k: (os.environ.get(v[1:], "") if v.startswith("$") else v) for k, v in agent_config.env.items()}

        try:
            from acp import spawn_agent_process

            # 启动 ACP 代理进程并执行完整的调用流程
            async with spawn_agent_process(client, cmd, *args, env=agent_env, cwd=physical_cwd) as (conn, proc):
                logger.info("Spawning ACP agent '%s' with command '%s' and args %s in cwd %s", agent, cmd, args, physical_cwd)
                # 初始化连接：协议版本、客户端能力、客户端信息
                await conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="deerflow", title="DeerFlow", version="0.1.0"),
                )
                # 创建会话：工作目录、MCP 服务器配置、可选模型
                session_kwargs: dict[str, Any] = {"cwd": physical_cwd, "mcp_servers": mcp_servers}
                if agent_config.model:
                    session_kwargs["model"] = agent_config.model
                session = await conn.new_session(**session_kwargs)
                # 发送提示
                await conn.prompt(
                    session_id=session.session_id,
                    prompt=[text_block(prompt)],
                )
            result = client.collected_text
            logger.info("ACP agent '%s' returned %s", agent, result[:1000])
            logger.info("ACP agent '%s' returned %d characters", agent, len(result))
            return result or "(no response)"
        except Exception as e:
            logger.error("ACP agent '%s' invocation failed: %s", agent, e)
            return _format_invocation_error(agent, cmd, e)

    return StructuredTool.from_function(
        name="invoke_acp_agent",
        description=description,
        coroutine=_invoke,
        args_schema=_InvokeACPAgentInput,
    )
