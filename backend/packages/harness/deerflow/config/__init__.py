"""配置系统公开接口。

本模块导出配置系统最常用的公共 API：
- get_app_config(): 获取应用配置（带缓存和热更新）
- get_paths(): 获取路径管理器单例
- get_extensions_config(): 获取扩展配置（MCP 服务器 + 技能状态）
- get_memory_config(): 获取记忆系统配置
- 追踪相关: get_tracing_config(), is_tracing_enabled() 等

配置系统内部结构：
- app_config.py: 根配置 AppConfig + 缓存/热更新/ContextVar 覆盖
- runtime_paths.py: 项目根目录和状态目录定位（最底层依赖）
- paths.py: 文件系统路径管理（线程目录、虚拟路径、Docker 挂载）
- extensions_config.py: MCP 服务器和技能状态配置
- model_config.py: LLM 模型声明
- sandbox_config.py: 沙箱系统配置
- database_config.py: 数据库后端配置
- 其他: 各子系统的配置模型（记忆、标题、摘要、工具、循环检测等）
"""

from .app_config import get_app_config
from .extensions_config import ExtensionsConfig, get_extensions_config
from .loop_detection_config import LoopDetectionConfig
from .memory_config import MemoryConfig, get_memory_config
from .paths import Paths, get_paths
from .skill_evolution_config import SkillEvolutionConfig
from .skills_config import SkillsConfig
from .tracing_config import (
    get_enabled_tracing_providers,
    get_explicitly_enabled_tracing_providers,
    get_tracing_config,
    is_tracing_enabled,
    validate_enabled_tracing_providers,
)

__all__ = [
    "get_app_config",
    "SkillEvolutionConfig",
    "Paths",
    "get_paths",
    "SkillsConfig",
    "ExtensionsConfig",
    "get_extensions_config",
    "LoopDetectionConfig",
    "MemoryConfig",
    "get_memory_config",
    "get_tracing_config",
    "get_explicitly_enabled_tracing_providers",
    "get_enabled_tracing_providers",
    "is_tracing_enabled",
    "validate_enabled_tracing_providers",
]
