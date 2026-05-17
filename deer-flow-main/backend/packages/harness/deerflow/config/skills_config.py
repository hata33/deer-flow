"""技能路径配置。

本模块定义了 DeerFlow 技能系统的目录路径配置，
包括技能在宿主机上的存储路径和沙箱容器内的挂载路径。

技能系统概述：
    技能是 DeerFlow 的扩展机制，通过 SKILL.md 文件定义。
    技能目录结构：
    ```
    skills/
    ├── public/          # 公共技能（提交到版本库）
    │   └── {skill_name}/
    │       └── SKILL.md
    └── custom/          # 自定义技能（gitignored）
        └── {skill_name}/
            └── SKILL.md
    ```

路径关系：
    - **宿主机路径** — 技能文件在宿主机上的实际位置（path 字段）。
    - **容器路径** — 技能在沙箱容器内的挂载位置（container_path 字段）。
    - 代理在沙箱内通过容器路径访问技能文件。

配置示例（config.yaml）：
    ```yaml
    skills:
      path: ../skills              # 宿主机技能目录
      container_path: /mnt/skills  # 容器内挂载路径
    ```
"""
from pathlib import Path

from pydantic import BaseModel, Field


class SkillsConfig(BaseModel):
    """技能系统路径配置。

    Attributes:
        path: 技能目录的宿主机路径。
            未指定时默认为 backend 目录的 ../skills。
            支持绝对路径和相对路径（相对于当前工作目录）。
        container_path: 技能在沙箱容器内的挂载路径。
            默认为 /mnt/skills。
    """

    path: str | None = Field(
        default=None,
        description="技能目录路径。未指定时默认为 backend 目录的 ../skills",
    )
    container_path: str = Field(
        default="/mnt/skills",
        description="技能在沙箱容器内的挂载路径",
    )

    def get_skills_path(self) -> Path:
        """获取解析后的技能目录路径。

        路径解析规则：
        - 已配置 path 且为绝对路径 → 直接使用
        - 已配置 path 且为相对路径 → 相对于当前工作目录解析
        - 未配置 path → 使用默认路径（通过 get_skills_root_path() 获取）

        Returns:
            解析后的技能目录绝对路径。
        """
        if self.path:
            path = Path(self.path)
            if not path.is_absolute():
                # 相对路径相对于当前工作目录解析
                path = Path.cwd() / path
            return path.resolve()
        else:
            # 默认路径：backend 目录的 ../skills
            from deerflow.skills.loader import get_skills_root_path

            return get_skills_root_path()

    def get_skill_container_path(self, skill_name: str, category: str = "public") -> str:
        """获取指定技能在容器内的完整路径。

        Args:
            skill_name: 技能名称（目录名）。
            category: 技能类别（"public" 或 "custom"）。

        Returns:
            容器内的完整路径（如 /mnt/skills/public/my-skill）。
        """
        return f"{self.container_path}/{category}/{skill_name}"
