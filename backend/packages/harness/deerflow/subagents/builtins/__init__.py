"""内置子代理配置注册表。

本模块定义了 DeerFlow 开箱即用的子代理集合。每个内置代理都是一个预配置的
SubagentConfig 实例，拥有针对特定场景优化的系统提示词和工具集。

内置代理:
    - general-purpose: 通用多步骤任务代理，继承除 task 外的全部工具
    - bash: 命令执行专家，仅使用沙箱文件操作工具

用户可在 config.yaml 的 subagents.custom_agents 段中添加自定义代理，
自定义代理与内置代理并列注册，由 registry.py 统一管理。
"""

from .bash_agent import BASH_AGENT_CONFIG
from .general_purpose import GENERAL_PURPOSE_CONFIG

__all__ = [
    "GENERAL_PURPOSE_CONFIG",
    "BASH_AGENT_CONFIG",
]

# 内置子代理注册表：名称 → SubagentConfig 映射
# registry.py 通过此字典查找内置代理配置
BUILTIN_SUBAGENTS = {
    "general-purpose": GENERAL_PURPOSE_CONFIG,
    "bash": BASH_AGENT_CONFIG,
}
