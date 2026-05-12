"""子智能体注册表，管理可用的子智能体。"""

import logging
from dataclasses import replace

from deerflow.sandbox.security import is_host_bash_allowed
from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
from deerflow.subagents.config import SubagentConfig

logger = logging.getLogger(__name__)


def get_subagent_config(name: str) -> SubagentConfig | None:
    """按名称获取子智能体配置，并应用 config.yaml 中的覆盖设置。

    Args:
        name: 子智能体名称。

    Returns:
        如果找到则返回 SubagentConfig（已应用 config.yaml 覆盖），否则返回 None。
    """
    config = BUILTIN_SUBAGENTS.get(name)
    if config is None:
        return None

    # 从 config.yaml 应用超时覆盖（延迟导入以避免循环依赖）
    from deerflow.config.subagents_config import get_subagents_app_config

    app_config = get_subagents_app_config()
    effective_timeout = app_config.get_timeout_for(name)
    if effective_timeout != config.timeout_seconds:
        logger.debug(f"Subagent '{name}': timeout overridden by config.yaml ({config.timeout_seconds}s -> {effective_timeout}s)")
        config = replace(config, timeout_seconds=effective_timeout)

    return config


def list_subagents() -> list[SubagentConfig]:
    """列出所有可用的子智能体配置（已应用 config.yaml 覆盖）。"""
    return [get_subagent_config(name) for name in BUILTIN_SUBAGENTS]


def get_subagent_names() -> list[str]:
    """获取所有可用的子智能体名称。"""
    return list(BUILTIN_SUBAGENTS.keys())


def get_available_subagent_names() -> list[str]:
    """获取在当前运行时中应暴露的子智能体名称。

    根据当前沙箱配置决定哪些子智能体可见（例如 bash 子智能体在主机
    不允许 bash 时会被隐藏）。
    """
    names = list(BUILTIN_SUBAGENTS.keys())
    try:
        host_bash_allowed = is_host_bash_allowed()
    except Exception:
        logger.debug("Could not determine host bash availability; exposing all built-in subagents")
        return names

    if not host_bash_allowed:
        names = [name for name in names if name != "bash"]
    return names
