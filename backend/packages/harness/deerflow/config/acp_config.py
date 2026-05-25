"""ACP 代理配置 — Agent Client Protocol 兼容的外部代理。

ACP（Agent Client Protocol）是一种标准化的代理间通信协议。
DeerFlow 可以作为 ACP 客户端调用外部 ACP 兼容代理，
将其能力作为工具暴露给主代理。

### 工作原理
1. 主代理通过 invoke_acp_agent 工具调用 ACP 代理
2. DeerFlow 启动 ACP 代理子进程（通过 command + args）
3. 通过 ACP 协议进行双向通信
4. ACP 代理的输出通过虚拟路径 /mnt/acp-workspace/ 返回给主代理

### 权限管理
ACP 代理可能在运行时请求权限（如文件访问）。
- auto_approve_permissions=true: 自动批准所有权限请求
- auto_approve_permissions=false（默认）: 拒绝所有权限请求
  代理必须配置为不请求权限

### 注意事项
- command 必须是真实的 ACP 适配器，不是原始的 CLI 工具
  例如使用 npx -y @zed-industries/codex-acp 而不是直接的 codex CLI
- 每个线程有独立的 ACP workspace，防止并发会话读取彼此的输出

本配置作为全局单例管理。
"""

import logging
from collections.abc import Mapping

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ACPAgentConfig(BaseModel):
    """单个 ACP 兼容代理的配置。

    - command: 启动 ACP 代理的命令
    - args: 命令参数
    - env: 注入的环境变量（$VAR 从宿主机环境变量解析）
    - description: 代理能力描述（显示在工具描述中）
    - model: 传递给代理的模型提示
    - auto_approve_permissions: 是否自动批准权限请求
    """

    command: str = Field(description="Command to launch the ACP agent subprocess")
    args: list[str] = Field(default_factory=list, description="Additional command arguments")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables to inject into the agent subprocess. Values starting with $ are resolved from host environment variables.")
    description: str = Field(description="Description of the agent's capabilities (shown in tool description)")
    model: str | None = Field(default=None, description="Model hint passed to the agent (optional)")
    auto_approve_permissions: bool = Field(
        default=False,
        description=(
            "When True, DeerFlow automatically approves all ACP permission requests from this agent "
            "(allow_once preferred over allow_always). When False (default), all permission requests "
            "are denied — the agent must be configured to operate without requesting permissions."
        ),
    )


# 全局单例 — 由 AppConfig._validate_acp_agents() + _apply_singleton_configs() 更新
_acp_agents: dict[str, ACPAgentConfig] = {}


def get_acp_agents() -> dict[str, ACPAgentConfig]:
    """获取当前配置的所有 ACP 代理。

    Returns:
        代理名 → ACPAgentConfig 映射。未配置时返回空字典。
    """
    return _acp_agents


def load_acp_config_from_dict(config_dict: Mapping[str, Mapping[str, object]] | None) -> None:
    """从字典加载 ACP 代理配置（由 AppConfig 初始化时调用）。

    config_dict 是 config.yaml 中 acp_agents 字段的内容：
    acp_agents:
      my-agent:
        command: npx
        args: ["-y", "@zed-industries/codex-acp"]
        description: "Code generation agent"
    """
    global _acp_agents
    if config_dict is None:
        config_dict = {}
    _acp_agents = {name: ACPAgentConfig(**cfg) for name, cfg in config_dict.items()}
    logger.info("ACP config loaded: %d agent(s): %s", len(_acp_agents), list(_acp_agents.keys()))
