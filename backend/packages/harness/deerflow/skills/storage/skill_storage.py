"""抽象 SkillStorage 基类 + 模板方法流程。

定义技能存储的抽象接口和跨后端复用的模板方法。
子类只需实现少量存储介质相关的原子操作，基类提供组合这些操作的
最终模板方法（如 ``load_skills``、路径助手、校验工具）。

设计模式: 模板方法 (Template Method)
  - **抽象方法**（子类必须实现）：存储介质相关的原子操作
    （如何遍历技能文件、如何读写文件、如何安装归档包）。
  - **具体方法**（基类提供）：跨后端复用的协议层逻辑
    （名称校验、相对路径校验、frontmatter 校验、技能发现与排序）。
  - 子类（如 ``LocalSkillStorage``）只关心"数据存在哪里"，
    基类处理"数据如何被消费"。

抽象方法清单
  - ``get_skills_root_path``: 技能根目录的宿主机绝对路径。
  - ``_iter_skill_files``: 遍历所有 ``SKILL.md`` 文件。
  - ``read_custom_skill`` / ``write_custom_skill``: 自定义技能的读写。
  - ``ainstall_skill_from_archive``: 从 .skill 归档包异步安装。
  - ``delete_custom_skill``: 删除自定义技能（含历史记录）。
  - ``custom_skill_exists`` / ``public_skill_exists``: 存在性检查。
  - ``append_history`` / ``read_history``: JSONL 历史记录读写。
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path

from deerflow.skills.types import SKILL_MD_FILE, Skill, SkillCategory  # noqa: F401

logger = logging.getLogger(__name__)

# 技能名称正则：连字符命名格式（小写字母+数字，连字符分隔）
_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillStorage(ABC):
    """技能存储后端的抽象基类。

    子类实现少量存储介质相关的原子操作；
    本基类提供组合这些操作的最终模板方法流程
    （load_skills、历史序列化、路径助手、校验），
    确保所有存储后端的行为一致。
    """

    def __init__(self, container_path: str = "/mnt/skills") -> None:
        self._container_root = container_path

    # ------------------------------------------------------------------
    # 静态协议助手（不依赖具体存储介质）
    # ------------------------------------------------------------------

    @staticmethod
    def validate_skill_name(name: str) -> str:
        """校验并规范化技能名称；返回规范化后的形式。

        规则：连字符命名格式（小写字母、数字、连字符），最长 64 字符。
        与 validation 模块的规则保持一致。
        """
        normalized = name.strip()
        if not _SKILL_NAME_PATTERN.fullmatch(normalized):
            raise ValueError(
                "Skill name must be hyphen-case using lowercase letters, digits, and hyphens only.")
        if len(normalized) > 64:
            raise ValueError("Skill name must be 64 characters or fewer.")
        return normalized

    @staticmethod
    def validate_relative_path(relative_path: str, base_dir: Path) -> Path:
        """校验 *relative_path* 相对于 *base_dir* 并返回解析后的目标路径。

        检查 *relative_path* 非空，然后与 *base_dir* 拼接并解析（跟随符号链接）。
        如果解析结果不在 *base_dir* 内，抛出 ``ValueError``（防目录穿越）。
        """
        if not relative_path:
            raise ValueError("relative_path must not be empty.")
        resolved_base = base_dir.resolve()
        target = (resolved_base / relative_path).resolve()
        try:
            target.relative_to(resolved_base)
        except ValueError as exc:
            raise ValueError(
                "relative_path must resolve within the skill directory.") from exc
        return target

    @staticmethod
    def validate_skill_markdown_content(name: str, content: str) -> None:
        """校验 SKILL.md 内容：解析 frontmatter 并检查 name 一致性。

        创建临时目录写入内容，然后委托给 ``_validate_skill_frontmatter``。
        额外检查 frontmatter 中的 name 与请求的 name 是否匹配。
        """
        import tempfile

        from deerflow.skills.validation import _validate_skill_frontmatter

        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_skill_dir = Path(tmp_dir) / \
                SkillStorage.validate_skill_name(name)
            temp_skill_dir.mkdir(parents=True, exist_ok=True)
            (temp_skill_dir / SKILL_MD_FILE).write_text(content, encoding="utf-8")
            is_valid, message, parsed_name = _validate_skill_frontmatter(
                temp_skill_dir)
            if not is_valid:
                raise ValueError(message)
            if parsed_name != name:
                raise ValueError(
                    f"Frontmatter name '{parsed_name}' must match requested skill name '{name}'.")

    def ensure_safe_support_path(self, name: str, relative_path: str) -> Path:
        """校验并返回辅助文件的解析后绝对路径。

        限制辅助文件只能存放在以下子目录中：
        ``references``、``templates``、``scripts``、``assets``。
        同时防止目录穿越攻击。
        """
        _ALLOWED_SUPPORT_SUBDIRS = {
            "references", "templates", "scripts", "assets"}
        skill_dir = self.get_custom_skill_dir(
            self.validate_skill_name(name)).resolve()
        if not relative_path or relative_path.endswith("/"):
            raise ValueError("Supporting file path must include a filename.")
        relative = Path(relative_path)
        if relative.is_absolute():
            raise ValueError("Supporting file path must be relative.")
        if any(part in {"..", ""} for part in relative.parts):
            raise ValueError(
                "Supporting file path must not contain parent-directory traversal.")
        top_level = relative.parts[0] if relative.parts else ""
        if top_level not in _ALLOWED_SUPPORT_SUBDIRS:
            raise ValueError(
                f"Supporting files must live under one of: {', '.join(sorted(_ALLOWED_SUPPORT_SUBDIRS))}.")
        target = (skill_dir / relative).resolve()
        allowed_root = (skill_dir / top_level).resolve()
        try:
            target.relative_to(allowed_root)
        except ValueError as exc:
            raise ValueError(
                "Supporting file path must stay within the selected support directory.") from exc
        return target

    # ------------------------------------------------------------------
    # 抽象原子操作（存储介质相关，子类必须实现）
    # ------------------------------------------------------------------

    @abstractmethod
    def get_skills_root_path(self) -> Path:
        """技能根目录的宿主机绝对路径，用于沙箱挂载。

        来源: ``deerflow.skills.loader.get_skills_root_path``。
        """

    @abstractmethod
    def _iter_skill_files(self) -> Iterable[tuple[SkillCategory, Path, Path]]:
        """遍历所有 SKILL.md 文件，生成 ``(category, category_root, skill_md_path)`` 三元组。

        来源: 提取自 ``deerflow.skills.loader.load_skills`` 中的目录遍历逻辑。
        """

    @abstractmethod
    def read_custom_skill(self, name: str) -> str:
        """读取自定义技能的 SKILL.md 内容。

        来源: ``deerflow.skills.manager.read_custom_skill_content``。
        """

    @abstractmethod
    def write_custom_skill(self, name: str, relative_path: str, content: str) -> None:
        """原子写入文本文件到 ``custom/<name>/<relative_path>``。

        来源: ``deerflow.skills.manager.atomic_write``。
        """

    @abstractmethod
    async def ainstall_skill_from_archive(self, archive_path: str | Path) -> dict:
        """从 ``.skill`` ZIP 归档包异步安装技能。

        来源: ``deerflow.skills.installer.ainstall_skill_from_archive``。
        """

    def install_skill_from_archive(self, archive_path: str | Path) -> dict:
        """同步包装器 —— 委托给 :meth:`ainstall_skill_from_archive`。

        处理事件循环检测：如果在运行中的事件循环内调用，
        则在单独线程中执行 ``asyncio.run()`` 以避免冲突。
        """
        from deerflow.skills.installer import _run_async_install

        return _run_async_install(self.ainstall_skill_from_archive(archive_path))

    @abstractmethod
    def delete_custom_skill(self, name: str, *, history_meta: dict | None = None) -> None:
        """删除自定义技能（校验 + 可选历史记录 + 目录删除）。

        来源: ``app.gateway.routers.skills.delete_custom_skill`` + ``skill_manage_tool``。
        """

    @abstractmethod
    def custom_skill_exists(self, name: str) -> bool:
        """检查自定义技能是否存在。

        来源: ``deerflow.skills.manager.custom_skill_exists``。
        """

    @abstractmethod
    def public_skill_exists(self, name: str) -> bool:
        """检查公共技能是否存在。

        来源: ``deerflow.skills.manager.public_skill_exists``。
        """

    @abstractmethod
    def append_history(self, name: str, record: dict) -> None:
        """为 ``name`` 追加一条 JSONL 历史记录。

        来源: ``deerflow.skills.manager.append_history``。
        """

    @abstractmethod
    def read_history(self, name: str) -> list[dict]:
        """返回 ``name`` 的所有历史记录，按时间从旧到新排列。

        来源: ``deerflow.skills.manager.read_history``。
        """

    # ------------------------------------------------------------------
    # 具体路径助手（布局是 SKILL.md 协议的一部分）
    # ------------------------------------------------------------------

    def get_container_root(self) -> str:
        """返回技能在容器中的挂载根路径。

        来源: ``deerflow.config.skills_config.SkillsConfig.container_path`` 访问器。
        """
        return self._container_root

    def get_custom_skill_dir(self, name: str) -> Path:
        """返回 ``custom/<name>`` 路径，不创建目录。

        来源: ``deerflow.skills.manager.get_custom_skill_dir``。
        """
        normalized_name = self.validate_skill_name(name)
        return self.get_skills_root_path() / SkillCategory.CUSTOM.value / normalized_name

    def get_custom_skill_file(self, name: str) -> Path:
        """返回 ``custom/<name>/SKILL.md`` 路径。

        来源: ``deerflow.skills.manager.get_custom_skill_file``。
        """
        normalized_name = self.validate_skill_name(name)
        return self.get_custom_skill_dir(normalized_name) / SKILL_MD_FILE

    def get_skill_history_file(self, name: str) -> Path:
        """返回 ``custom/.history/<name>.jsonl`` 路径，不创建父目录。

        来源: ``deerflow.skills.manager.get_skill_history_file``。
        """
        normalized_name = self.validate_skill_name(name)
        return self.get_skills_root_path() / SkillCategory.CUSTOM.value / ".history" / f"{normalized_name}.jsonl"

    # ------------------------------------------------------------------
    # 最终模板方法流程
    # ------------------------------------------------------------------

    def load_skills(self, *, enabled_only: bool = False) -> list[Skill]:
        """发现所有技能，合并启用状态，排序并可选过滤。

        模板方法流程:
            1. 通过 ``_iter_skill_files()`` 遍历所有 SKILL.md。
            2. 对每个文件调用 ``parse_skill_file()`` 解析。
            3. 按 name 去重（同名的 custom 技能覆盖 public 技能）。
            4. 从 ``extensions_config.json`` 合并 ``enabled`` 状态。
               （每次调用都重新读取，确保其他进程的更改立即生效）。
            5. 按名称字母序排序。
            6. 如果 ``enabled_only=True``，仅返回已启用的技能。

        Args:
            enabled_only: 是否仅返回已启用的技能。

        Returns:
            技能列表（按名称排序）。

        来源: ``deerflow.skills.loader.load_skills``。
        """
        from deerflow.skills.parser import parse_skill_file

        skills_by_name: dict[str, Skill] = {}
        for category, category_root, md_path in self._iter_skill_files():
            skill = parse_skill_file(
                md_path,
                category=category,
                relative_path=md_path.parent.relative_to(category_root),
            )
            if skill:
                skills_by_name[skill.name] = skill

        skills = list(skills_by_name.values())

        # 从扩展配置合并启用状态（每次调用都重新读取，
        # 确保其他进程的更改能立即生效）。
        try:
            from deerflow.config.extensions_config import ExtensionsConfig

            extensions_config = ExtensionsConfig.from_file()
            for skill in skills:
                skill.enabled = extensions_config.is_skill_enabled(
                    skill.name, skill.category)
        except Exception as e:
            logger.warning("Failed to load extensions config: %s", e)

        if enabled_only:
            skills = [s for s in skills if s.enabled]

        skills.sort(key=lambda s: s.name)
        return skills

    def ensure_custom_skill_is_editable(self, name: str) -> None:
        """确保指定名称的自定义技能可编辑。

        规则:
        - 如果自定义技能已存在 → 通过。
        - 如果同名公共技能存在 → 报错，提示用户在 custom/ 下创建。
        - 如果都不存在 → 报 FileNotFoundError。

        来源: ``deerflow.skills.manager.ensure_custom_skill_is_editable``。
        """
        if self.custom_skill_exists(name):
            return
        if self.public_skill_exists(name):
            raise ValueError(
                f"'{name}' is a built-in skill. To customise it, create a new skill with the same name under skills/custom/.")
        raise FileNotFoundError(f"Custom skill '{name}' not found.")
