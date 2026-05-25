"""技能系统配置 — 技能目录定位与容器路径。

技能（Skills）是以 SKILL.md 定义的 Agent 能力扩展包。
本配置控制技能目录的位置解析，包括宿主机路径和容器内路径。

### 路径解析优先级
1. config.yaml 中的显式 path 字段
2. DEER_FLOW_SKILLS_PATH 环境变量
3. 项目根目录下的 skills/ 目录
4. 传统 monorepo 位置的兼容查找

### 容器路径
在沙箱容器中，技能通过卷挂载映射到 container_path（默认 /mnt/skills）。
Agent 在沙箱内通过此路径访问技能文件。

### 路径解析委托
路径解析委托给 runtime_paths 模块，确保与项目根目录定位一致。
"""

import os
from pathlib import Path

from pydantic import BaseModel, Field

from deerflow.config.runtime_paths import project_root, resolve_path


def _legacy_skills_candidates() -> tuple[Path, ...]:
    """返回传统 monorepo 中的技能目录位置，用于向后兼容。"""
    backend_dir = Path(__file__).resolve().parents[4]
    repo_root = backend_dir.parent
    return (repo_root / "skills",)


class SkillsConfig(BaseModel):
    """技能系统配置。

    - use: SkillStorage 实现类的路径
    - path: 宿主机上的技能目录路径（空=自动检测）
    - container_path: 沙箱容器中的技能挂载路径
    """

    use: str = Field(
        default="deerflow.skills.storage.local_skill_storage:LocalSkillStorage",
        description="Class path of the SkillStorage implementation.",
    )
    path: str | None = Field(
        default=None,
        description=("Path to skills directory. If not specified, defaults to `skills` under the caller project root, falling back to the legacy repo-root location for monorepo compatibility."),
    )
    container_path: str = Field(
        default="/mnt/skills",
        description="Path where skills are mounted in the sandbox container",
    )

    def get_skills_path(self) -> Path:
        """解析技能目录的绝对路径。

        解析顺序：
        1. 显式 path 字段（绝对路径直接使用，相对路径基于项目根解析）
        2. DEER_FLOW_SKILLS_PATH 环境变量
        3. 项目根目录下的 skills/ 目录（如果存在）
        4. 传统 monorepo 位置的兼容查找
        5. 都找不到时返回项目根目录下的 skills/（即使不存在，也给出稳定位置）
        """
        if self.path:
            # 用户配置了显式路径
            return resolve_path(self.path)
        if env_path := os.getenv("DEER_FLOW_SKILLS_PATH"):
            return resolve_path(env_path)

        # 自动检测：项目根目录下的 skills/
        project_default = project_root() / "skills"
        if project_default.is_dir():
            return project_default

        # 兼容传统 monorepo 布局
        for candidate in _legacy_skills_candidates():
            if candidate.is_dir():
                return candidate

        # 返回默认路径（即使不存在），让调用方能给出稳定的"无技能"位置
        return project_default

    def get_skill_container_path(self, skill_name: str, category: str = "public") -> str:
        """获取技能在容器内的完整路径。

        格式：{container_path}/{category}/{skill_name}
        例如：/mnt/skills/public/code-review
        """
        return f"{self.container_path}/{category}/{skill_name}"
