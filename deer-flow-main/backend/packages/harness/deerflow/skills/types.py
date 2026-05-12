"""技能数据类型定义。

定义 Skill 数据类，包含技能的元数据、文件路径、
容器路径映射等属性。
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    """技能数据类，表示一个已加载的技能。

    每个技能对应一个包含 SKILL.md 文件的目录，
    通过 YAML frontmatter 声明名称、描述、许可等元数据。

    Attributes:
        name: 技能唯一名称（hyphen-case 格式）。
        description: 技能功能描述。
        license: 许可证信息，可选。
        skill_dir: 技能目录的宿主机绝对路径。
        skill_file: SKILL.md 文件的宿主机绝对路径。
        relative_path: 相对于类别根目录（skills/public 或 skills/custom）的路径。
        category: 技能类别，'public'（公共）或 'custom'（自定义）。
        enabled: 是否启用，默认 False，由 extensions_config.json 控制。
    """

    name: str
    description: str
    license: str | None
    skill_dir: Path
    skill_file: Path
    relative_path: Path  # 相对于类别根目录的路径
    category: str  # 'public' 或 'custom'
    enabled: bool = False  # 是否启用，实际状态来自配置文件

    @property
    def skill_path(self) -> str:
        """返回相对于类别根目录的路径字符串，根目录本身返回空串。"""
        path = self.relative_path.as_posix()
        return "" if path == "." else path

    def get_container_path(self, container_base_path: str = "/mnt/skills") -> str:
        """获取技能目录在容器中的挂载路径。

        Args:
            container_base_path: 容器中技能的挂载基础路径。

        Returns:
            容器中技能目录的完整路径。
        """
        category_base = f"{container_base_path}/{self.category}"
        skill_path = self.skill_path
        if skill_path:
            return f"{category_base}/{skill_path}"
        return category_base

    def get_container_file_path(self, container_base_path: str = "/mnt/skills") -> str:
        """获取技能 SKILL.md 文件在容器中的完整路径。

        Args:
            container_base_path: 容器中技能的挂载基础路径。

        Returns:
            容器中 SKILL.md 文件的完整路径。
        """
        return f"{self.get_container_path(container_base_path)}/SKILL.md"

    def __repr__(self) -> str:
        return f"Skill(name={self.name!r}, description={self.description!r}, category={self.category!r})"
