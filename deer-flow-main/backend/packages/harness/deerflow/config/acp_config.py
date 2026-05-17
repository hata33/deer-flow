"""ACP（Agent Communication Protocol）代理配置模块。

本模块负责加载和管理 ACP 兼容的外部代理配置。
ACP 是一种标准化的代理通信协议，允许 DeerFlow 通过子进程启动并控制外部代理。

核心概念：
    - **ACP 代理** — 独立运行的外部代理进程，通过 ACP 协议与 DeerFlow 通信。
    - **command** — 启动代理子进程的命令（如 ``npx -y @zed-industries/codex-acp``）。
    - **auto_approve_permissions** — 是否自动批准代理的权限请求。

配置来源：
    从 config.yaml 的 ``acp_agents`` 字段加载，格式为：
    ```yaml
    acp_agents:
      codex:
        command: npx
        args: ["-y", "@zed-industries/codex-acp"]
        description: "Code generation agent"
        model: gpt-4o
        auto_approve_permissions: false
        env:
          OPENAI_API_KEY: $OPENAI_API_KEY
    ```

数据流：
    1. AppConfig.from_file() 读取 config.yaml
    2. 调用 load_acp_config_from_dict() 解析 acp_agents 字段
    3. get_acp_agents() 返回全局配置字典
    4. 运行时通过 invoke_acp_agent 工具调用已配置的代理

注意：
    - ACP 启动器必须是真正的 ACP 适配器，标准 codex CLI 本身不兼容 ACP 协议。
    - 每个代理使用独立的线程工作空间（per-thread workspace），路径为
      ``{base_dir}/threads/{thread_id}/acp-workspace/``。
    - env 字段中以 ``$`` 开头的值会从宿主机环境变量解析。
"""
import logging
from collections.abc import Mapping

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ACPAgentConfig(BaseModel):
    """单个 ACP 兼容代理的配置。

    对应 config.yaml 中 ``acp_agents.<name>`` 下的配置项。

    Attributes:
        command: 启动代理子进程的命令（如 ``npx``、``python``）。
        args: 传递给命令的额外参数列表。
        env: 注入到代理子进程的环境变量。值以 ``$`` 开头的会从宿主机环境解析。
        description: 代理能力描述（显示在工具描述中，帮助 Lead Agent 决定何时调用）。
        model: 可选的模型提示，传递给代理用于选择推理模型。
        auto_approve_permissions: 是否自动批准代理的权限请求。
            True 时使用 allow_once 而非 allow_always；
            False（默认）时所有权限请求被拒绝——代理必须配置为无需请求权限即可运行。
    """

    command: str = Field(description="启动代理子进程的命令")
    args: list[str] = Field(default_factory=list, description="传递给命令的额外参数列表")
    env: dict[str, str] = Field(
        default_factory=dict,
        description="注入到代理子进程的环境变量。值以 $ 开头的会从宿主机环境解析。",
    )
    description: str = Field(description="代理能力描述（显示在工具描述中）")
    model: str | None = Field(default=None, description="传递给代理的模型提示（可选）")
    auto_approve_permissions: bool = Field(
        default=False,
        description=(
            "是否自动批准代理的权限请求。"
            "True 时使用 allow_once 而非 allow_always；"
            "False（默认）时所有权限请求被拒绝——代理必须配置为无需请求权限即可运行。"
        ),
    )


# ── 全局配置实例 ──────────────────────────────────────────────────────────
# 键为代理名称（config.yaml 中的字段名），值为对应的 ACPAgentConfig
_acp_agents: dict[str, ACPAgentConfig] = {}


def get_acp_agents() -> dict[str, ACPAgentConfig]:
    """获取当前已配置的 ACP 代理映射。

    Returns:
        代理名称 → ACPAgentConfig 的字典。如果未配置任何 ACP 代理，返回空字典。
    """
    return _acp_agents


def load_acp_config_from_dict(config_dict: Mapping[str, Mapping[str, object]] | None) -> None:
    """从字典加载 ACP 代理配置（通常来自 config.yaml 的 acp_agents 字段）。

    每次调用会完全替换之前的配置（而非合并），确保移除的条目不会残留。

    Args:
        config_dict: 代理名称 → 配置字段的映射。为 None 时视为空字典。
    """
    global _acp_agents
    if config_dict is None:
        config_dict = {}
    _acp_agents = {name: ACPAgentConfig(**cfg) for name, cfg in config_dict.items()}
    logger.info("ACP config loaded: %d agent(s): %s", len(_acp_agents), list(_acp_agents.keys()))
