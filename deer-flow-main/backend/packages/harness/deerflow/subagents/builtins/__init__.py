"""内置子智能体配置。

包含通用型子智能体和 bash 命令执行子智能体的配置。
"""

from .bash_agent import BASH_AGENT_CONFIG
from .general_purpose import GENERAL_PURPOSE_CONFIG

__all__ = [
    "GENERAL_PURPOSE_CONFIG",  # 通用型子智能体配置
    "BASH_AGENT_CONFIG",  # bash 命令执行子智能体配置
]

# 内置子智能体注册表
BUILTIN_SUBAGENTS = {
    "general-purpose": GENERAL_PURPOSE_CONFIG,
    "bash": BASH_AGENT_CONFIG,
}
