"""SKILL.md 文件解析器。

从 SKILL.md 文件的 YAML frontmatter 中提取元数据（name、description、license），
构建 Skill 数据类实例。frontmatter 格式为简单的 key: value 行，无需完整 YAML 解析器。
"""

import logging
import re
from pathlib import Path

from .types import Skill

logger = logging.getLogger(__name__)


def parse_skill_file(skill_file: Path, category: str, relative_path: Path | None = None) -> Skill | None:
    """解析 SKILL.md 文件，提取元数据并构建 Skill 实例。

    仅识别 YAML frontmatter 区域（--- 包围的部分）中的 key: value 行，
    不解析嵌套 YAML 结构。缺少 name 或 description 字段时返回 None。

    Args:
        skill_file: SKILL.md 文件的路径。
        category: 技能类别（'public' 或 'custom'）。
        relative_path: 相对于类别根目录的路径。

    Returns:
        解析成功的 Skill 实例，解析失败返回 None。
    """
    if not skill_file.exists() or skill_file.name != "SKILL.md":
        return None

    try:
        content = skill_file.read_text(encoding="utf-8")

        # 提取 YAML frontmatter（--- 包围的区域）
        front_matter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)

        if not front_matter_match:
            return None

        front_matter = front_matter_match.group(1)

        # 简单 key: value 解析（无需完整 YAML 解析器）
        metadata = {}
        for line in front_matter.split("\n"):
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip()

        # name 和 description 为必填字段
        name = metadata.get("name")
        description = metadata.get("description")

        if not name or not description:
            return None

        license_text = metadata.get("license")

        return Skill(
            name=name,
            description=description,
            license=license_text,
            skill_dir=skill_file.parent,
            skill_file=skill_file,
            relative_path=relative_path or Path(skill_file.parent.name),
            category=category,
            enabled=True,  # 默认启用，实际状态由配置文件覆盖
        )

    except Exception as e:
        logger.error("Error parsing skill file %s: %s", skill_file, e)
        return None
