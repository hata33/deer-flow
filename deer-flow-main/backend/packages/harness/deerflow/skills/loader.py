"""技能加载器。

扫描 public 和 custom 目录树，发现所有包含 SKILL.md 的子目录，
解析元数据并合并 extensions_config.json 中的启用状态。
支持按启用状态过滤，返回排序后的技能列表。

注意：启用状态使用 ExtensionsConfig.from_file() 每次从磁盘读取，
确保 Gateway API（独立进程）的修改能立即反映到 LangGraph Server。
"""

import logging
import os
from pathlib import Path

from .parser import parse_skill_file
from .types import Skill

logger = logging.getLogger(__name__)


def get_skills_root_path() -> Path:
    """获取技能根目录的默认路径（deer-flow/skills）。

    通过文件位置反推：loader.py 位于 packages/harness/deerflow/skills/，
    向上 5 级到达 backend/，再取父目录的 skills/ 子目录。

    Returns:
        技能根目录路径。
    """
    # loader.py 位于 packages/harness/deerflow/skills/loader.py
    # 向上 5 级: loader.py → skills/ → deerflow/ → harness/ → packages/ → backend/
    backend_dir = Path(__file__).resolve().parent.parent.parent.parent.parent
    # skills 目录与 backend 目录平级
    skills_dir = backend_dir.parent / "skills"
    return skills_dir


def load_skills(skills_path: Path | None = None, use_config: bool = True, enabled_only: bool = False) -> list[Skill]:
    """扫描并加载所有技能。

    遍历 skills_path 下的 public/ 和 custom/ 目录，递归查找所有
    包含 SKILL.md 文件的子目录，解析元数据并合并启用状态。

    Args:
        skills_path: 自定义技能目录路径，为 None 时从配置或默认路径加载。
        use_config: 是否从应用配置中读取技能路径，默认 True。
        enabled_only: 是否只返回已启用的技能，默认 False。

    Returns:
        按名称排序的 Skill 实例列表。
    """
    if skills_path is None:
        if use_config:
            try:
                from deerflow.config import get_app_config

                config = get_app_config()
                skills_path = config.skills.get_skills_path()
            except Exception:
                # 配置加载失败时回退到默认路径
                skills_path = get_skills_root_path()
        else:
            skills_path = get_skills_root_path()

    if not skills_path.exists():
        return []

    skills = []

    # 扫描 public 和 custom 两个类别目录
    for category in ["public", "custom"]:
        category_path = skills_path / category
        if not category_path.exists() or not category_path.is_dir():
            continue

        for current_root, dir_names, file_names in os.walk(category_path, followlinks=True):
            # 保持遍历确定性，跳过隐藏目录
            dir_names[:] = sorted(name for name in dir_names if not name.startswith("."))
            if "SKILL.md" not in file_names:
                continue

            skill_file = Path(current_root) / "SKILL.md"
            relative_path = skill_file.parent.relative_to(category_path)

            skill = parse_skill_file(skill_file, category=category, relative_path=relative_path)
            if skill:
                skills.append(skill)

    # 从磁盘读取最新的启用状态配置
    # 使用 ExtensionsConfig.from_file() 而非缓存的 get_extensions_config()，
    # 确保 Gateway API（独立进程）的修改能立即反映到 LangGraph Server
    try:
        from deerflow.config.extensions_config import ExtensionsConfig

        extensions_config = ExtensionsConfig.from_file()
        for skill in skills:
            skill.enabled = extensions_config.is_skill_enabled(skill.name, skill.category)
    except Exception as e:
        # 配置加载失败时默认全部启用
        logger.warning("Failed to load extensions config: %s", e)

    # 按启用状态过滤
    if enabled_only:
        skills = [skill for skill in skills if skill.enabled]

    # 按名称排序，确保输出顺序一致
    skills.sort(key=lambda s: s.name)

    return skills
