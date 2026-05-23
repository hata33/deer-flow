"""技能系统 — DeerFlow Agent 的可插拔扩展机制。

技能是基于 Markdown 的扩展包（``SKILL.md`` + 可选的辅助文件），
用于教会 Agent 如何执行特定任务。本模块提供技能全生命周期管理：
解析、校验、安全扫描、归档包安装、工具策略执行、存储抽象。

公开接口
--------
- ``Skill`` — 表示已加载技能及其元数据的数据类。
- ``SkillStorage`` / ``LocalSkillStorage`` — 抽象存储基类与本地文件系统实现。
- ``get_or_new_skill_storage`` — 单例/按需存储工厂函数。
- ``_validate_skill_frontmatter`` — 无需完整加载 Skill 对象即可校验 SKILL.md。
- ``ALLOWED_FRONTMATTER_PROPERTIES`` — 允许的 frontmatter 字段白名单。
- ``SkillAlreadyExistsError`` / ``SkillSecurityScanError`` — 类型化错误哨兵。
"""

from __future__ import annotations

from .installer import SkillAlreadyExistsError, SkillSecurityScanError
from .storage import LocalSkillStorage, SkillStorage, get_or_new_skill_storage
from .types import Skill
from .validation import ALLOWED_FRONTMATTER_PROPERTIES, _validate_skill_frontmatter

__all__ = [
    "Skill",
    "ALLOWED_FRONTMATTER_PROPERTIES",
    "_validate_skill_frontmatter",
    "SkillAlreadyExistsError",
    "SkillSecurityScanError",
    "SkillStorage",
    "LocalSkillStorage",
    "get_or_new_skill_storage",
]
