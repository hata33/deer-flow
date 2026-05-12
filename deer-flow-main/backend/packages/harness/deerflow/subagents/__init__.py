"""子智能体（Subagent）模块。

提供子智能体的配置、注册、执行等能力，支持将复杂任务委托给
独立的子智能体进行并行处理。
"""

from .config import SubagentConfig
from .executor import SubagentExecutor, SubagentResult
from .registry import get_available_subagent_names, get_subagent_config, list_subagents

__all__ = [
    "SubagentConfig",  # 子智能体配置数据类
    "SubagentExecutor",  # 子智能体执行器
    "SubagentResult",  # 子智能体执行结果
    "get_available_subagent_names",  # 获取当前运行时可用的子智能体名称列表
    "get_subagent_config",  # 按名称获取子智能体配置
    "list_subagents",  # 列出所有可用的子智能体配置
]
