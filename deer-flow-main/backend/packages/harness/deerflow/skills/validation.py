"""技能 frontmatter 校验工具。

纯逻辑校验——无 FastAPI 或 HTTP 依赖。
Gateway 和 Client 共用此模块进行 SKILL.md 格式验证。

校验规则：
- 必须包含 YAML frontmatter（--- 包围区域）
- name 和 description 为必填字段
- name 必须为 hyphen-case（小写字母、数字、连字符）
- description 不能包含尖括号，长度不超过 1024 字符
- 不允许未定义的属性键
"""

import re
from pathlib import Path

import yaml

# 允许的 frontmatter 属性集合
ALLOWED_FRONTMATTER_PROPERTIES = {"name", "description", "license", "allowed-tools", "metadata", "compatibility", "version", "author"}


def _validate_skill_frontmatter(skill_dir: Path) -> tuple[bool, str, str | None]:
    """校验技能目录中 SKILL.md 的 frontmatter 格式。

    执行完整的多层校验：文件存在性、frontmatter 格式、属性白名单、
    必填字段、命名规范、长度限制等。

    Args:
        skill_dir: 包含 SKILL.md 的技能目录路径。

    Returns:
        三元组 (is_valid, message, skill_name)：
        - is_valid: 是否通过校验
        - message: 校验结果描述或错误详情
        - skill_name: 解析出的技能名称，校验失败时为 None
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found", None

    content = skill_md.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return False, "No YAML frontmatter found", None

    # 提取并解析 frontmatter
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format", None

    frontmatter_text = match.group(1)

    try:
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, "Frontmatter must be a YAML dictionary", None
    except yaml.YAMLError as e:
        return False, f"Invalid YAML in frontmatter: {e}", None

    # 检查未定义的属性键
    unexpected_keys = set(frontmatter.keys()) - ALLOWED_FRONTMATTER_PROPERTIES
    if unexpected_keys:
        return False, f"Unexpected key(s) in SKILL.md frontmatter: {', '.join(sorted(unexpected_keys))}", None

    # 校验必填字段
    if "name" not in frontmatter:
        return False, "Missing 'name' in frontmatter", None
    if "description" not in frontmatter:
        return False, "Missing 'description' in frontmatter", None

    # 校验 name 格式
    name = frontmatter.get("name", "")
    if not isinstance(name, str):
        return False, f"Name must be a string, got {type(name).__name__}", None
    name = name.strip()
    if not name:
        return False, "Name cannot be empty", None

    # name 必须为 hyphen-case（小写字母、数字、连字符）
    if not re.match(r"^[a-z0-9-]+$", name):
        return False, f"Name '{name}' should be hyphen-case (lowercase letters, digits, and hyphens only)", None
    # 不允许首尾连字符或连续连字符
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return False, f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens", None
    # 名称长度限制
    if len(name) > 64:
        return False, f"Name is too long ({len(name)} characters). Maximum is 64 characters.", None

    # 校验 description 格式
    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return False, f"Description must be a string, got {type(description).__name__}", None
    description = description.strip()
    if description:
        # 禁止尖括号（防止 prompt injection）
        if "<" in description or ">" in description:
            return False, "Description cannot contain angle brackets (< or >)", None
        if len(description) > 1024:
            return False, f"Description is too long ({len(description)} characters). Maximum is 1024 characters.", None

    return True, "Skill is valid!", name
