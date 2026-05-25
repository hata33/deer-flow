"""子代理系统配置 — 任务委派与并发控制。

子代理系统允许主代理（Lead Agent）将子任务委派给专门的代理执行。
本配置控制：
- 全局超时和最大轮次
- 按代理名称的个别覆盖（per-agent override）
- 用户自定义代理类型

### 配置层级
1. 内置默认值（代码中的硬编码）
2. 全局配置（timeout_seconds, max_turns）
3. 按代理覆盖（agents 字段中对应代理名的设置）
4. 自定义代理（custom_agents 字段中声明的全新代理类型）

查询顺序：per-agent override → 全局默认 → 内置默认
"""

import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SubagentOverrideConfig(BaseModel):
    """按代理的配置覆盖。

    每个字段为 None 时表示不覆盖（使用全局或内置默认值）。
    例如只覆盖超时而不改变模型：

        agents:
          general-purpose:
            timeout_seconds: 1800  # 30 分钟
    """

    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Timeout in seconds for this subagent (None = use global default)",
    )
    max_turns: int | None = Field(
        default=None,
        ge=1,
        description="Maximum turns for this subagent (None = use global or builtin default)",
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        description="Model name for this subagent (None = inherit from parent agent)",
    )
    skills: list[str] | None = Field(
        default=None,
        description="Skill names whitelist for this subagent (None = inherit all enabled skills, [] = no skills)",
    )


class CustomSubagentConfig(BaseModel):
    """用户自定义子代理类型。

    在 config.yaml 中声明全新的代理类型，DeerFlow 自动注册为可用的子代理。
    用户可以定制：
    - system_prompt: 代理的行为指导（必需）
    - tools/disallowed_tools: 工具白名单/黑名单
    - skills: 技能白名单
    - model: 使用的模型（inherit 表示继承父代理的模型）
    - max_turns/timeout_seconds: 执行限制

    默认情况下，自定义代理不允许使用 task、ask_clarification、present_files 工具，
    以避免无限递归委派和不必要的交互。
    """

    description: str = Field(
        description="When the lead agent should delegate to this subagent",
    )
    system_prompt: str = Field(
        description="System prompt that guides the subagent's behavior",
    )
    tools: list[str] | None = Field(
        default=None,
        description="Tool names whitelist (None = inherit all tools from parent)",
    )
    disallowed_tools: list[str] | None = Field(
        default_factory=lambda: ["task", "ask_clarification", "present_files"],
        description="Tool names to deny",
    )
    skills: list[str] | None = Field(
        default=None,
        description="Skill names whitelist (None = inherit all enabled skills, [] = no skills)",
    )
    model: str = Field(
        default="inherit",
        description="Model to use - 'inherit' uses parent's model",
    )
    max_turns: int = Field(
        default=50,
        ge=1,
        description="Maximum number of agent turns before stopping",
    )
    timeout_seconds: int = Field(
        default=900,
        ge=1,
        description="Maximum execution time in seconds",
    )


class SubagentsAppConfig(BaseModel):
    """子代理系统全局配置。

    - timeout_seconds: 全局默认超时（默认 15 分钟）
    - max_turns: 全局默认最大轮次（None 表示使用内置默认）
    - agents: 按代理名的覆盖配置
    - custom_agents: 用户自定义的代理类型
    """

    timeout_seconds: int = Field(
        default=900,
        ge=1,
        description="Default timeout in seconds for all subagents (default: 900 = 15 minutes)",
    )
    max_turns: int | None = Field(
        default=None,
        ge=1,
        description="Optional default max-turn override for all subagents (None = keep builtin defaults)",
    )
    agents: dict[str, SubagentOverrideConfig] = Field(
        default_factory=dict,
        description="Per-agent configuration overrides keyed by agent name",
    )
    custom_agents: dict[str, CustomSubagentConfig] = Field(
        default_factory=dict,
        description="User-defined subagent types keyed by agent name",
    )

    def get_timeout_for(self, agent_name: str) -> int:
        """获取指定代理的有效超时时间。

        查询顺序：per-agent override.timeout_seconds → 全局 timeout_seconds
        """
        override = self.agents.get(agent_name)
        if override is not None and override.timeout_seconds is not None:
            return override.timeout_seconds
        return self.timeout_seconds

    def get_model_for(self, agent_name: str) -> str | None:
        """获取指定代理的模型覆盖。

        返回 None 表示继承父代理的模型。
        """
        override = self.agents.get(agent_name)
        if override is not None and override.model is not None:
            return override.model
        return None

    def get_max_turns_for(self, agent_name: str, builtin_default: int) -> int:
        """获取指定代理的有效最大轮次。

        查询顺序：per-agent override.max_turns → 全局 max_turns → 内置默认
        """
        override = self.agents.get(agent_name)
        if override is not None and override.max_turns is not None:
            return override.max_turns
        if self.max_turns is not None:
            return self.max_turns
        return builtin_default

    def get_skills_for(self, agent_name: str) -> list[str] | None:
        """获取指定代理的技能白名单覆盖。

        返回 None 表示继承所有启用的技能。
        """
        override = self.agents.get(agent_name)
        if override is not None and override.skills is not None:
            return override.skills
        return None


# 全局单例 — 由 AppConfig._apply_singleton_configs() 在加载时更新
_subagents_config: SubagentsAppConfig = SubagentsAppConfig()


def get_subagents_app_config() -> SubagentsAppConfig:
    """获取当前子代理配置（全局单例）。"""
    return _subagents_config


def load_subagents_config_from_dict(config_dict: dict) -> None:
    """从字典加载子代理配置（由 AppConfig 初始化时调用）。

    加载后记录覆盖和自定义代理的摘要到日志，方便排查配置问题。
    """
    global _subagents_config
    _subagents_config = SubagentsAppConfig(**config_dict)

    # 构建覆盖摘要用于日志
    overrides_summary = {}
    for name, override in _subagents_config.agents.items():
        parts = []
        if override.timeout_seconds is not None:
            parts.append(f"timeout={override.timeout_seconds}s")
        if override.max_turns is not None:
            parts.append(f"max_turns={override.max_turns}")
        if override.model is not None:
            parts.append(f"model={override.model}")
        if override.skills is not None:
            parts.append(f"skills={override.skills}")
        if parts:
            overrides_summary[name] = ", ".join(parts)

    custom_agents_names = list(_subagents_config.custom_agents.keys())

    if overrides_summary or custom_agents_names:
        logger.info(
            "Subagents config loaded: default timeout=%ss, default max_turns=%s, per-agent overrides=%s, custom_agents=%s",
            _subagents_config.timeout_seconds,
            _subagents_config.max_turns,
            overrides_summary or "none",
            custom_agents_names or "none",
        )
