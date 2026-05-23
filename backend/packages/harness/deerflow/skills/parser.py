"""SKILL.md frontmatter 解析器。

解析 ``SKILL.md`` 文件中的 YAML frontmatter 块并构建 :class:`Skill` 对象。
这是**唯一**读取原始技能元数据的代码路径 —— 所有上层模块（加载器、管理器、API）
都消费已解析的 ``Skill`` 实例。

解析策略
  - frontmatter 块必须是文件的第一部分内容，由 ``---`` 围栏分隔（Markdown + YAML 标准约定）。
  - 缺失或格式错误的 frontmatter → 返回 ``None``（该技能被静默跳过）。
  - 缺失必需字段（``name``、``description``）→ 返回 ``None``。
  - 无效的 ``allowed-tools`` → 记录错误日志并返回 ``None``。
  - 意外异常 → 记录日志并返回 ``None``（绝不因单个技能损坏而导致加载器崩溃）。

设计理由
  - **软失败**：损坏的 SKILL.md 不应阻止 Agent 启动，该技能仅被排除在可用列表之外。
  - **严格字段校验**：``name`` 和 ``description`` 是必需的，因为无名称或无描述
    的技能会让 Agent 和用户都感到困惑。
  - **allowed-tools 可选**：省略表示"不限制工具"（兼容旧行为），
    显式空列表表示"该技能禁止使用任何工具"。
"""

import logging
import re
from pathlib import Path

import yaml

from .types import SKILL_MD_FILE, Skill, SkillCategory

logger = logging.getLogger(__name__)


def parse_allowed_tools(raw: object, skill_file: Path) -> list[str] | None:
    """解析可选的 ``allowed-tools`` frontmatter 字段。

    **Null 与空列表的区别**：
      - ``None``（字段未声明）→ 不限制，所有工具可用。
      - ``[]``（显式空列表）→ 该技能授予*零*个工具的访问权限。

    这一区别对工具策略组合至关重要：一旦有*任意*一个已加载的技能声明了
    ``allowed-tools``，未声明该字段的技能不会向工具并集中贡献任何内容
    （见 :func:`deerflow.skills.tool_policy.allowed_tool_names_for_skills`）。

    Args:
        raw: YAML frontmatter 中 ``allowed-tools`` 键的值。
        skill_file: SKILL.md 文件路径（仅用于错误消息）。

    Returns:
        字段省略时返回 ``None``；字段为 YAML 字符串序列时返回列表（可能为空）。

    Raises:
        ValueError: 值不是列表，或包含非字符串元素。
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(
            f"allowed-tools in {skill_file} must be a list of strings")

    allowed_tools: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(
                f"allowed-tools in {skill_file} must contain only strings")
        tool_name = item.strip()
        if not tool_name:
            raise ValueError(
                f"allowed-tools in {skill_file} cannot contain empty tool names")
        allowed_tools.append(tool_name)
    return allowed_tools


def parse_skill_file(skill_file: Path, category: SkillCategory, relative_path: Path | None = None) -> Skill | None:
    """解析 ``SKILL.md`` 文件并返回完整填充的 :class:`Skill` 对象。

    这是技能元数据提取的**唯一入口**。执行以下步骤：

    1. 校验文件存在且文件名为 ``SKILL.md``。
    2. 读取文件并提取 ``---`` 围栏之间的 YAML frontmatter。
    3. 解析必需字段（``name``、``description``）和可选字段
       （``license``、``allowed-tools``）。
    4. 规范化空白字符并返回 ``Skill`` 数据类实例。

    **错误处理**：任何失败（文件不存在、YAML 格式错误、缺少 name、
    无效的 allowed-tools）都返回 ``None`` —— 调用方（通常是
    ``SkillStorage.load_skills``）会静默跳过该技能。

    Args:
        skill_file: ``SKILL.md`` 文件的路径。
        category: ``SkillCategory.PUBLIC`` 或 ``SkillCategory.CUSTOM``。
        relative_path: 从分类根目录到技能目录的相对路径，省略时默认使用技能目录名。

    Returns:
        解析成功时返回 ``Skill`` 对象，否则返回 ``None``。
    """
    # 文件不存在或文件名不是 SKILL.md → 跳过
    if not skill_file.exists() or skill_file.name != SKILL_MD_FILE:
        return None

    try:
        content = skill_file.read_text(encoding="utf-8")

        # 提取 ``---`` 围栏之间的 YAML frontmatter 块。
        # 使用 DOTALL 使多行值中的换行也能被 ``.`` 匹配。
        front_matter_match = re.match(
            r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not front_matter_match:
            return None

        front_matter_text = front_matter_match.group(1)

        try:
            metadata = yaml.safe_load(front_matter_text)
        except yaml.YAMLError as exc:
            logger.error("Invalid YAML front-matter in %s: %s",
                         skill_file, exc)
            return None

        if not isinstance(metadata, dict):
            logger.error(
                "Front-matter in %s is not a YAML mapping", skill_file)
            return None

        # 提取必需字段 —— 两者都必须是非空字符串。
        name = metadata.get("name")
        description = metadata.get("description")

        if not name or not isinstance(name, str):
            return None
        if not description or not isinstance(description, str):
            return None

        # 规范化：去除 YAML 可能保留的首尾空白。
        name = name.strip()
        description = description.strip()

        if not name or not description:
            return None

        # 可选字段：许可证
        license_text = metadata.get("license")
        if license_text is not None:
            license_text = str(license_text).strip() or None

        # 可选字段：工具白名单
        try:
            allowed_tools = parse_allowed_tools(
                metadata.get("allowed-tools"), skill_file)
        except ValueError as exc:
            logger.error("Invalid allowed-tools in %s: %s", skill_file, exc)
            return None

        return Skill(
            name=name,
            description=description,
            license=license_text,
            skill_dir=skill_file.parent,
            skill_file=skill_file,
            relative_path=relative_path or Path(skill_file.parent.name),
            category=category,
            allowed_tools=allowed_tools,
            enabled=True,  # 实际启用状态由扩展配置文件决定，此处默认为 True。
        )

    except Exception:
        logger.exception("Unexpected error parsing skill file %s", skill_file)
        return None
