"""子智能体（Subagent）系统配置。

本模块定义了 DeerFlow 子智能体系统的配置参数。
子智能体允许 Lead Agent 将任务委托给专门的代理执行。

核心概念：
    - **子智能体** — 独立运行的专门代理，由 Lead Agent 通过 task 工具调用。
    - **超时机制** — 每个子智能体有执行超时限制，防止长时间运行的任务阻塞系统。
    - **覆盖机制** — 支持为特定子智能体设置独立的超时覆盖。

超时优先级：
    1. 如果子智能体在 agents 中有配置且 timeout_seconds 不为 None → 使用覆盖值
    2. 否则 → 使用全局默认值 timeout_seconds（默认 900 秒 = 15 分钟）

内置子智能体：
    - general-purpose — 通用代理（拥有除 task 外的所有工具）
    - bash — 命令行专家代理

执行架构：
    双线程池设计：
    - _scheduler_pool（3 个 worker）— 调度任务
    - _execution_pool（3 个 worker）— 执行任务
    - MAX_CONCURRENT_SUBAGENTS = 3 — 由 SubagentLimitMiddleware 强制执行

配置示例（config.yaml）：
    ```yaml
    subagents:
      timeout_seconds: 900
      agents:
        general-purpose:
          timeout_seconds: 600
        bash:
          timeout_seconds: 300
    ```
"""
import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SubagentOverrideConfig(BaseModel):
    """单个子智能体的配置覆盖。

    用于为特定子智能体设置不同于全局默认值的参数。

    Attributes:
        timeout_seconds: 该子智能体的执行超时（秒）。
            None 表示使用全局默认值。
    """

    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description="该子智能体的超时时间（秒）。None 表示使用全局默认值。",
    )


class SubagentsAppConfig(BaseModel):
    """子智能体系统配置。

    Attributes:
        timeout_seconds: 所有子智能体的默认超时时间（秒）。默认 900 = 15 分钟。
        agents: 按智能体名称索引的配置覆盖映射。
    """

    timeout_seconds: int = Field(
        default=900,
        ge=1,
        description="所有子智能体的默认超时时间（秒）（默认 900 = 15 分钟）",
    )
    agents: dict[str, SubagentOverrideConfig] = Field(
        default_factory=dict,
        description="按智能体名称索引的配置覆盖",
    )

    def get_timeout_for(self, agent_name: str) -> int:
        """获取指定子智能体的有效超时时间。

        优先使用该智能体的覆盖配置，否则使用全局默认值。

        Args:
            agent_name: 子智能体名称。

        Returns:
            超时时间（秒）。
        """
        override = self.agents.get(agent_name)
        if override is not None and override.timeout_seconds is not None:
            return override.timeout_seconds
        return self.timeout_seconds


# ── 全局配置实例 ──────────────────────────────────────────────────────────
_subagents_config: SubagentsAppConfig = SubagentsAppConfig()


def get_subagents_app_config() -> SubagentsAppConfig:
    """获取当前子智能体配置。"""
    return _subagents_config


def load_subagents_config_from_dict(config_dict: dict) -> None:
    """从字典加载子智能体配置（由 AppConfig.from_file 调用）。

    加载后会记录日志，包含默认超时和各智能体的覆盖情况。
    """
    global _subagents_config
    _subagents_config = SubagentsAppConfig(**config_dict)

    # 记录配置摘要
    overrides_summary = {name: f"{override.timeout_seconds}s" for name, override in _subagents_config.agents.items() if override.timeout_seconds is not None}
    if overrides_summary:
        logger.info(f"Subagents config loaded: default timeout={_subagents_config.timeout_seconds}s, per-agent overrides={overrides_summary}")
    else:
        logger.info(f"Subagents config loaded: default timeout={_subagents_config.timeout_seconds}s, no per-agent overrides")
