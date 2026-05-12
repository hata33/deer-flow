"""ACP（Agent Communication Protocol）外部代理调用工具。

通过 ACP 协议与外部兼容代理交互，支持流式文本收集和权限自动审批。
每个线程拥有独立的工作空间（acp-workspace），代理的输出文件可通过
/mnt/acp-workspace/ 路径被主代理读取（只读）。
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
    """ACP 代理调用工具的输入参数定义。"""

    agent: str = Field(description="Name of the ACP agent to invoke")
    prompt: str = Field(description="The concise task prompt to send to the agent")


def _get_work_dir(thread_id: str | None) -> str:
    """获取当前线程的 ACP 工作空间目录。

    每个线程在 ``{base_dir}/threads/{thread_id}/acp-workspace/`` 下
    拥有独立的工作空间，确保并发会话之间互不干扰。

    当 thread_id 不可用时（如嵌入式/直接调用），回退到全局目录
    ``{base_dir}/acp-workspace/``。

    目录不存在时自动创建。

    Returns:
        工作空间的绝对物理文件系统路径。
    """
    from deerflow.config.paths import get_paths

    paths = get_paths()
    if thread_id:
        try:
            work_dir = paths.acp_workspace_dir(thread_id)
        except ValueError:
            logger.warning("Invalid thread_id %r for ACP workspace, falling back to global", thread_id)
            work_dir = paths.base_dir / "acp-workspace"
    else:
        work_dir = paths.base_dir / "acp-workspace"

    work_dir.mkdir(parents=True, exist_ok=True)
    logger.info("ACP agent work_dir: %s", work_dir)
    return str(work_dir)


def _build_mcp_servers() -> dict[str, dict[str, Any]]:
    """从 DeerFlow 已启用的 MCP 服务器构建 ACP mcpServers 配置。"""
    from deerflow.config.extensions_config import ExtensionsConfig
    from deerflow.mcp.client import build_servers_config

    return build_servers_config(ExtensionsConfig.from_file())


def _build_permission_response(options: list[Any], *, auto_approve: bool) -> Any:
    """构建 ACP 权限响应。

    当 ``auto_approve`` 为 True 时，选择第一个 ``allow_once``（优先）
    或 ``allow_always`` 选项。为 False（默认）时，始终拒绝——权限请求
    必须由 ACP 代理自身的策略处理，或配置代理为无需请求权限模式。
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
    """生成面向用户的 ACP 调用错误信息，附带可操作的修复建议。"""
    if not isinstance(exc, FileNotFoundError):
        return f"Error invoking ACP agent '{agent}': {exc}"

    # 命令未找到，提供安装指引
    message = f"Error invoking ACP agent '{agent}': Command '{cmd}' was not found on PATH."
    if cmd == "codex-acp" and shutil.which("codex"):
        # codex CLI 不直接支持 ACP 协议，提示安装适配器
        return f"{message} The installed `codex` CLI does not speak ACP directly. Install a Codex ACP adapter (for example `npx @zed-industries/codex-acp`) or update `acp_agents.codex.command` and `args` in config.yaml."

    return f"{message} Install the agent binary or update `acp_agents.{agent}.command` in config.yaml."


def build_invoke_acp_agent_tool(agents: dict) -> BaseTool:
    """创建 ``invoke_acp_agent`` 工具，工具描述根据已配置的代理动态生成。

    工具描述中包含可用代理列表，使 LLM 能了解可调用的代理而无需硬编码名称。

    Args:
        agents: 代理名称到 ``ACPAgentConfig`` 的映射。

    Returns:
        可直接加入工具列表的 LangChain ``BaseTool``。
    """
    # 动态生成工具描述，列出所有可用代理
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

    # 通过闭包捕获代理配置，使内部函数可以引用
    _agents = dict(agents)

    async def _invoke(agent: str, prompt: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
        """ACP 代理的实际调用逻辑：建立连接、创建会话、发送提示并收集响应。"""
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
            """最小化 ACP 客户端，收集会话更新中的流式文本。"""

            def __init__(self) -> None:
                self._chunks: list[str] = []

            @property
            def collected_text(self) -> str:
                return "".join(self._chunks)

            async def session_update(self, session_id: str, update, **kwargs) -> None:  # type: ignore[override]
                """接收会话更新，提取 TextContentBlock 中的文本块。"""
                try:
                    from acp.schema import TextContentBlock

                    if hasattr(update, "content") and isinstance(update.content, TextContentBlock):
                        self._chunks.append(update.content.text)
                except Exception:
                    pass

            async def request_permission(self, options, session_id: str, tool_call, **kwargs):  # type: ignore[override]
                """处理 ACP 代理的权限请求，根据配置自动审批或拒绝。"""
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
        mcp_servers = _build_mcp_servers()
        # 构建代理环境变量（以 $ 开头的值从系统环境变量解析）
        agent_env: dict[str, str] | None = None
        if agent_config.env:
            agent_env = {k: (os.environ.get(v[1:], "") if v.startswith("$") else v) for k, v in agent_config.env.items()}

        try:
            from acp import spawn_agent_process

            # 启动 ACP 代理进程并建立连接
            async with spawn_agent_process(client, cmd, *args, env=agent_env, cwd=physical_cwd) as (conn, proc):
                logger.info("Spawning ACP agent '%s' with command '%s' and args %s in cwd %s", agent, cmd, args, physical_cwd)
                await conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="deerflow", title="DeerFlow", version="0.1.0"),
                )
                session_kwargs: dict[str, Any] = {"cwd": physical_cwd, "mcp_servers": mcp_servers}
                if agent_config.model:
                    session_kwargs["model"] = agent_config.model
                session = await conn.new_session(**session_kwargs)
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
