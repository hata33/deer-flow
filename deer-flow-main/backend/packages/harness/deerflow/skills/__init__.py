"""技能（Skills）模块。

提供技能的发现、加载、解析、验证和安装功能。
技能以 SKILL.md 文件为核心，采用 YAML frontmatter 定义元数据，
支持 public（公共）和 custom（自定义）两种类别。

主要组件：
- types: Skill 数据类定义
- parser: SKILL.md 文件解析器
- loader: 技能扫描与加载（遍历目录树，合并配置状态）
- validation: frontmatter 格式校验
- installer: .skill 归档文件的安全解压与安装
"""

from .installer import SkillAlreadyExistsError, install_skill_from_archive
from .loader import get_skills_root_path, load_skills
from .types import Skill
from .validation import ALLOWED_FRONTMATTER_PROPERTIES, _validate_skill_frontmatter

__all__ = [
    "load_skills",                  # 扫描并加载所有技能
    "get_skills_root_path",         # 获取技能根目录路径
    "Skill",                        # 技能数据类
    "ALLOWED_FRONTMATTER_PROPERTIES",  # 允许的 frontmatter 属性集合
    "_validate_skill_frontmatter",  # frontmatter 校验函数
    "install_skill_from_archive",   # 从 .skill 归档安装技能
    "SkillAlreadyExistsError",      # 技能已存在异常
]
