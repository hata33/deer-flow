"""子代理注册与发现模块。

本模块管理所有可用的子代理配置，包括内置代理（general-purpose、bash）
和用户在 config.yaml 中定义的自定义代理。提供按名称查找、列举所有代理、
以及应用 config.yaml 覆盖等功能。

配置解析优先级（镜像 Codex 的配置分层）:
    1. 内置子代理定义（BUILTIN_SUBAGENTS 字典）
    2. config.yaml custom_agents 段的自定义代理
    3. config.yaml agents 段的 per-agent 覆盖（timeout, max_turns, model, skills）
    4. config.yaml 全局默认值（仅对内置代理生效，不影响自定义代理的自有默认值）

沙箱过滤:
    当主机 bash 不可用时（sandbox 配置限制），get_available_subagent_names()
    会自动隐藏 bash 子代理，确保前端和 task() 工具仅暴露当前运行时可用的代理。
"""

import logging
from dataclasses import replace
from typing import Any

from deerflow.sandbox.security import is_host_bash_allowed
from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
from deerflow.subagents.config import SubagentConfig

logger = logging.getLogger(__name__)


def _resolve_subagents_app_config(app_config: Any | None = None):
    """解析子代理应用配置对象。

    支持传入完整的 AppConfig 或专用的 SubagentsAppConfig。
    当 app_config 为 None 时，自动加载全局配置。

    Args:
        app_config: 可选的应用配置对象。

    Returns:
        SubagentsAppConfig 实例。
    """
    if app_config is None:
        from deerflow.config.subagents_config import get_subagents_app_config

        return get_subagents_app_config()
    return getattr(app_config, "subagents", app_config)


def _build_custom_subagent_config(name: str, *, app_config: Any | None = None) -> SubagentConfig | None:
    """从 config.yaml 的 custom_agents 段构建自定义代理配置。

    读取 config.yaml 中 subagents.custom_agents 部分的指定代理定义，
    将其转换为 SubagentConfig 数据类实例。

    Args:
        name: 自定义代理名称。
        app_config: 可选的应用配置对象。

    Returns:
        如果在 custom_agents 中找到匹配项，返回 SubagentConfig；
        否则返回 None。
    """
    subagents_config = _resolve_subagents_app_config(app_config)
    custom = subagents_config.custom_agents.get(name)
    if custom is None:
        return None

    return SubagentConfig(
        name=name,
        description=custom.description,
        system_prompt=custom.system_prompt,
        tools=custom.tools,
        disallowed_tools=custom.disallowed_tools,
        skills=custom.skills,
        model=custom.model,
        max_turns=custom.max_turns,
        timeout_seconds=custom.timeout_seconds,
    )


def get_subagent_config(name: str, *, app_config: Any | None = None) -> SubagentConfig | None:
    """按名称查找子代理配置，并应用 config.yaml 覆盖。

    解析顺序（镜像 Codex 的配置分层）:
    1. 首先查找内置子代理（general-purpose、bash）
    2. 如果未找到，查找 config.yaml custom_agents 段的自定义代理
    3. 应用 config.yaml agents 段的 per-agent 覆盖和全局默认值

    覆盖规则:
    - timeout_seconds: per-agent 覆盖 > 全局默认（仅内置代理）> 配置自身值
    - max_turns: per-agent 覆盖 > 全局默认（仅内置代理）> 配置自身值
    - model: 仅 per-agent 覆盖（无全局默认）
    - skills: 仅 per-agent 覆盖（无全局默认）
    - 全局默认值不影响自定义代理的自有值

    Args:
        name: 子代理名称。
        app_config: 可选的应用配置对象。

    Returns:
        如果找到，返回应用了覆盖的 SubagentConfig；否则返回 None。
    """
    # 第一步：查找内置代理，然后回退到 custom_agents
    config = BUILTIN_SUBAGENTS.get(name)
    if config is None:
        config = _build_custom_subagent_config(name, app_config=app_config)
    if config is None:
        return None

    # 第二步：应用 config.yaml agents 段的 per-agent 覆盖
    # 仅应用显式的 per-agent 覆盖。全局默认值（顶层的 timeout_seconds、max_turns）
    # 适用于内置代理，但不得覆盖自定义代理自身的值——自定义代理在
    # custom_agents 段定义了自己的默认值。
    subagents_config = _resolve_subagents_app_config(app_config)
    is_builtin = name in BUILTIN_SUBAGENTS
    agent_override = subagents_config.agents.get(name)

    overrides = {}

    # 超时: per-agent 覆盖 > 全局默认（仅内置代理）> 配置自身值
    if agent_override is not None and agent_override.timeout_seconds is not None:
        if agent_override.timeout_seconds != config.timeout_seconds:
            logger.debug("Subagent '%s': timeout overridden (%ss -> %ss)", name, config.timeout_seconds, agent_override.timeout_seconds)
            overrides["timeout_seconds"] = agent_override.timeout_seconds
    elif is_builtin and subagents_config.timeout_seconds != config.timeout_seconds:
        logger.debug("Subagent '%s': timeout from global default (%ss -> %ss)", name, config.timeout_seconds, subagents_config.timeout_seconds)
        overrides["timeout_seconds"] = subagents_config.timeout_seconds

    # 最大轮次: per-agent 覆盖 > 全局默认（仅内置代理）> 配置自身值
    if agent_override is not None and agent_override.max_turns is not None:
        if agent_override.max_turns != config.max_turns:
            logger.debug("Subagent '%s': max_turns overridden (%s -> %s)", name, config.max_turns, agent_override.max_turns)
            overrides["max_turns"] = agent_override.max_turns
    elif is_builtin and subagents_config.max_turns is not None and subagents_config.max_turns != config.max_turns:
        logger.debug("Subagent '%s': max_turns from global default (%s -> %s)", name, config.max_turns, subagents_config.max_turns)
        overrides["max_turns"] = subagents_config.max_turns

    # 模型: 仅 per-agent 覆盖（无全局默认）
    effective_model = subagents_config.get_model_for(name)
    if effective_model is not None and effective_model != config.model:
        logger.debug("Subagent '%s': model overridden (%s -> %s)", name, config.model, effective_model)
        overrides["model"] = effective_model

    # 技能: 仅 per-agent 覆盖（无全局默认）
    effective_skills = subagents_config.get_skills_for(name)
    if effective_skills is not None and effective_skills != config.skills:
        logger.debug("Subagent '%s': skills overridden (%s -> %s)", name, config.skills, effective_skills)
        overrides["skills"] = effective_skills

    if overrides:
        config = replace(config, **overrides)

    return config


def list_subagents(*, app_config: Any | None = None) -> list[SubagentConfig]:
    """列出所有可用的子代理配置（已应用 config.yaml 覆盖）。

    合并内置代理和自定义代理，对每个代理调用 get_subagent_config()
    应用相应的覆盖。

    Returns:
        所有已注册的 SubagentConfig 实例列表（内置 + 自定义）。
    """
    configs = []
    for name in get_subagent_names(app_config=app_config):
        config = get_subagent_config(name, app_config=app_config)
        if config is not None:
            configs.append(config)
    return configs


def get_subagent_names(*, app_config: Any | None = None) -> list[str]:
    """获取所有可用的子代理名称（内置 + 自定义）。

    先收集内置代理名称，然后合并 config.yaml custom_agents 段中
    的自定义代理名称，避免重复。

    Returns:
        子代理名称列表。
    """
    names = list(BUILTIN_SUBAGENTS.keys())

    # 合并 config.yaml 中的自定义代理
    subagents_config = _resolve_subagents_app_config(app_config)
    for custom_name in subagents_config.custom_agents:
        if custom_name not in names:
            names.append(custom_name)

    return names


def get_available_subagent_names(*, app_config: Any | None = None) -> list[str]:
    """获取当前运行时应该暴露给调用方的子代理名称。

    在 get_subagent_names() 的基础上增加沙箱过滤:
    - 如果主机 bash 不可用（sandbox 配置限制），隐藏 bash 子代理
    - 如果无法确定 bash 可用性，暴露全部代理（保守策略）

    Returns:
        当前运行时可用的子代理名称列表。
    """
    names = get_subagent_names(app_config=app_config)
    try:
        host_bash_allowed = is_host_bash_allowed(app_config) if hasattr(app_config, "sandbox") else is_host_bash_allowed()
    except Exception:
        logger.debug("Could not determine host bash availability; exposing all subagents")
        return names

    if not host_bash_allowed:
        names = [name for name in names if name != "bash"]
    return names
