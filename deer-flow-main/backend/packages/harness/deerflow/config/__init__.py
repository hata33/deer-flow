"""配置系统入口模块。

本模块是 DeerFlow 配置系统的统一导出入口（facade），
外部代码只需 ``from deerflow.config import ...`` 即可获取所需的配置实例。

导出内容概览：
    - **应用配置** — `get_app_config()` 返回 `AppConfig` 单例，支持文件热重载和 mtime 检测。
    - **路径配置** — `Paths` / `get_paths()` 管理所有数据目录布局和虚拟路径映射。
    - **技能配置** — `SkillsConfig` 定义技能目录的宿主机与容器挂载路径。
    - **扩展配置** — `ExtensionsConfig` / `get_extensions_config()` 管理 MCP 服务器和技能启停状态。
    - **记忆配置** — `MemoryConfig` / `get_memory_config()` 控制记忆系统的存储、注入和更新行为。
    - **追踪配置** — `get_tracing_config()` / `is_tracing_enabled()` 封装 LangSmith 追踪的环境变量读取。

设计原则：
    - 每个子模块维护自己的全局单例（懒加载 / 可重置），本模块仅做重新导出。
    - 其他子配置（如 checkpointer、sandbox、model 等）由 `AppConfig` 统一加载和管理，
      不在此处直接导出，避免顶层依赖过重。
"""

# 应用主配置（AppConfig）——所有配置的根入口
from .app_config import get_app_config

# 扩展配置——MCP 服务器 + 技能启停状态
from .extensions_config import ExtensionsConfig, get_extensions_config

# 记忆系统配置
from .memory_config import MemoryConfig, get_memory_config

# 路径管理——集中管理所有数据目录和虚拟路径映射
from .paths import Paths, get_paths

# 技能路径配置
from .skills_config import SkillsConfig

# LangSmith 追踪配置
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
