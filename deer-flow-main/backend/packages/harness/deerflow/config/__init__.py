"""配置系统入口模块。

提供应用配置、扩展配置、路径、记忆配置、追踪配置等核心配置的统一访问入口。
"""

from .app_config import get_app_config
from .extensions_config import ExtensionsConfig, get_extensions_config
from .memory_config import MemoryConfig, get_memory_config
from .paths import Paths, get_paths
from .skills_config import SkillsConfig
from .tracing_config import get_tracing_config, is_tracing_enabled

__all__ = [
    "get_app_config",           # 获取应用配置（缓存 + 热重载）
    "Paths",                    # 路径配置类
    "get_paths",                # 获取全局路径单例
    "SkillsConfig",             # 技能路径配置
    "ExtensionsConfig",         # MCP + 技能状态配置
    "get_extensions_config",    # 获取扩展配置
    "MemoryConfig",             # 记忆系统配置
    "get_memory_config",        # 获取记忆配置
    "get_tracing_config",       # 获取追踪配置
    "is_tracing_enabled",       # 判断是否启用追踪
]
