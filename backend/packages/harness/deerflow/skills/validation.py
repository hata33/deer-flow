"""技能 frontmatter 校验工具。

对 SKILL.md 的 frontmatter 进行纯逻辑校验 —— 不依赖 FastAPI 或 HTTP。
在校验规则上比 :mod:`parser` 更严格：不仅检查字段是否存在，
还校验命名规范、值合法性、长度限制、以及是否包含未预期的属性键。

校验规则总览
  - 文件必须存在且以 ``---`` 开头（有 YAML frontmatter）。
  - frontmatter 必须是合法的 YAML 字典。
  - 不允许出现白名单之外的键（防止拼写错误和注入）。
  - ``name`` 和 ``description`` 是必填字段。
  - ``name`` 必须是连字符命名格式（小写字母、数字、连字符），最长 64 字符。
  - ``description`` 不能包含尖括号（防 XSS），最长 1024 字符。
  - ``allowed-tools`` 委托给 :func:`parser.parse_allowed_tools` 校验。

与 parser 的关系
  ``parser.parse_skill_file()`` 是宽松的发现阶段解析 —— 坏技能被静默跳过。
  本模块的 ``_validate_skill_frontmatter()`` 是严格的交互阶段校验 ——
  返回明确的错误信息给用户（如 API 上传、CLI 安装等场景）。
"""

import re
from pathlib import Path

import yaml

from deerflow.skills.parser import parse_allowed_tools
from deerflow.skills.types import SKILL_MD_FILE

# SKILL.md frontmatter 中允许出现的属性白名单。
# 防止用户无意中写入不被识别的键（拼写错误等）。
ALLOWED_FRONTMATTER_PROPERTIES = {"name", "description", "license",
                                  "allowed-tools", "metadata", "compatibility", "version", "author"}


def _validate_skill_frontmatter(skill_dir: Path) -> tuple[bool, str, str | None]:
    """校验技能目录中 SKILL.md 的 frontmatter。

    这是交互式场景下的严格校验入口（如上传安装、API 编辑）。
    与 ``parse_skill_file()`` 的软失败策略不同，此处返回详细的错误消息。

    校验流程:
        1. 检查 SKILL.md 文件是否存在。
        2. 检查是否以 ``---`` 开头（存在 YAML frontmatter）。
        3. 提取并解析 YAML frontmatter。
        4. 检查是否存在未预期的属性键（白名单校验）。
        5. 校验 ``name``：必填、字符串、非空、连字符命名、≤64 字符、不能首尾或连续连字符。
        6. 校验 ``description``：字符串、非空、不含尖括号、≤1024 字符。
        7. 委托 ``parse_allowed_tools`` 校验 ``allowed-tools`` 字段。

    Args:
        skill_dir: 包含 SKILL.md 的技能目录路径。

    Returns:
        ``(is_valid, message, skill_name)`` 三元组：
        - ``is_valid``: 校验是否通过。
        - ``message``: 成功或失败的描述信息。
        - ``skill_name``: 解析到的技能名称（失败时为 None）。
    """
    skill_md = skill_dir / SKILL_MD_FILE
    if not skill_md.exists():
        return False, f"{SKILL_MD_FILE} not found", None

    content = skill_md.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return False, "No YAML frontmatter found", None

    # 提取 frontmatter 块（``---`` 围栏之间的内容）
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format", None

    frontmatter_text = match.group(1)

    # 解析 YAML frontmatter
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, "Frontmatter must be a YAML dictionary", None
    except yaml.YAMLError as e:
        return False, f"Invalid YAML in frontmatter: {e}", None

    # 白名单校验：检查是否存在未预期的属性键
    unexpected_keys = set(frontmatter.keys()) - ALLOWED_FRONTMATTER_PROPERTIES
    if unexpected_keys:
        return False, f"Unexpected key(s) in SKILL.md frontmatter: {', '.join(sorted(unexpected_keys))}", None

    # 必填字段检查
    if "name" not in frontmatter:
        return False, "Missing 'name' in frontmatter", None
    if "description" not in frontmatter:
        return False, "Missing 'description' in frontmatter", None

    # 校验 name：类型、非空、命名规范、长度
    name = frontmatter.get("name", "")
    if not isinstance(name, str):
        return False, f"Name must be a string, got {type(name).__name__}", None
    name = name.strip()
    if not name:
        return False, "Name cannot be empty", None

    # 连字符命名规范：只允许小写字母、数字、连字符
    if not re.match(r"^[a-z0-9-]+$", name):
        return False, f"Name '{name}' should be hyphen-case (lowercase letters, digits, and hyphens only)", None
    # 不能以连字符开头/结尾，不能有连续连字符
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return False, f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens", None
    if len(name) > 64:
        return False, f"Name is too long ({len(name)} characters). Maximum is 64 characters.", None

    # 校验 description：类型、防 XSS、长度限制
    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return False, f"Description must be a string, got {type(description).__name__}", None
    description = description.strip()
    if description:
        if "<" in description or ">" in description:
            return False, "Description cannot contain angle brackets (< or >)", None
        if len(description) > 1024:
            return False, f"Description is too long ({len(description)} characters). Maximum is 1024 characters.", None

    # 委托 parser 校验 allowed-tools 格式
    try:
        parse_allowed_tools(frontmatter.get("allowed-tools"), skill_md)
    except ValueError as e:
        return False, str(e).replace(str(skill_md), SKILL_MD_FILE), None

    return True, "Skill is valid!", name
