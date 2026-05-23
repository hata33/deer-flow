"""技能系统核心类型定义。

定义了规范的 ``Skill`` 数据类和 ``SkillCategory`` 枚举。
这些类型贯穿技能全生命周期 —— 从发现/解析，到运行时注入和工具策略过滤。

``Skill.enabled`` 的设计考量
  ``enabled`` 标记**不会**持久化到 SKILL.md 中。它在加载时从外部的
  ``extensions_config.json`` 合并而来，这样做的好处是：启用/禁用开关
  在技能升级后依然保留，且不需要修改技能源码。
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# 每个技能目录必须包含的规范文件名。
SKILL_MD_FILE = "SKILL.md"


class SkillCategory(StrEnum):
    """技能的来源分类。

    - ``PUBLIC``: 平台内置技能，只读，不可编辑或删除。
      存放在 ``<skills_root>/public/`` 目录下。
    - ``CUSTOM``: 用户创建或安装的技能，可以编辑、删除、版本跟踪。
      存放在 ``<skills_root>/custom/`` 目录下。
    """

    PUBLIC = "public"
    CUSTOM = "custom"


@dataclass
class Skill:
    """已发现技能的完整解析表示。

    由 ``parse_skill_file()`` 从 ``SKILL.md`` 文件解析生成，
    加载时从扩展配置中合并 ``enabled`` 状态。

    属性说明:
        name: 连字符命名格式的技能标识符（如 ``"code-review"``）。
        description: frontmatter 中的人类可读摘要。
        license: 可选的 SPDX 标识符或许可证文本。
        skill_dir: 宿主机文件系统上技能目录的路径。
        skill_file: 宿主机文件系统上 ``SKILL.md`` 文件的路径。
        relative_path: 从分类根目录到技能目录的相对路径。
        category: ``SkillCategory.PUBLIC`` 或 ``SkillCategory.CUSTOM``。
        allowed_tools: 显式工具白名单（``None`` = 不限制，``[]`` = 禁止所有工具）。
        enabled: 技能是否激活（从扩展配置合并而来）。
    """

    name: str
    description: str
    license: str | None
    skill_dir: Path
    skill_file: Path
    relative_path: Path  # 从分类根目录到技能目录的相对路径
    category: SkillCategory  # 'public' 或 'custom'
    allowed_tools: list[str] | None = None
    enabled: bool = False  # 技能是否已启用

    @property
    def skill_path(self) -> str:
        """从分类根目录到此技能目录的相对路径。

        用于构建沙箱挂载路径和在分类中标识技能。
        对于直接放在分类根目录下的技能返回 ``""``（实践中很少见）。
        """
        path = self.relative_path.as_posix()
        return "" if path == "." else path

    def get_container_path(self, container_base_path: str = "/mnt/skills") -> str:
        """计算技能目录在沙箱容器中的路径。

        技能通过 bind mount 挂载到 Agent 沙箱的固定基础路径下。
        此方法返回完整的容器路径，供工具运行器和运行时引用技能文件。

        Args:
            container_base_path: 技能在容器中的挂载根路径。

        Returns:
            技能目录的完整容器路径，如 ``"/mnt/skills/public/code-review"``。
        """
        category_base = f"{container_base_path}/{self.category}"
        skill_path = self.skill_path
        if skill_path:
            return f"{category_base}/{skill_path}"
        return category_base

    def get_container_file_path(self, container_base_path: str = "/mnt/skills") -> str:
        """计算技能主文件（SKILL.md）在沙箱容器中的路径。

        便捷方法，在 :meth:`get_container_path` 结果后追加 ``/SKILL.md``。
        技能加载器在构建列出可用技能的 system prompt 时使用此方法。

        Args:
            container_base_path: 技能在容器中的挂载根路径。

        Returns:
            技能 SKILL.md 文件的完整容器路径。
        """
        return f"{self.get_container_path(container_base_path)}/SKILL.md"

    def __repr__(self) -> str:
        """紧凑的调试表示 —— 省略文件系统路径以提高可读性。"""
        return f"Skill(name={self.name!r}, description={self.description!r}, category={self.category!r})"
