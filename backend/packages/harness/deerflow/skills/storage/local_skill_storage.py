"""SkillStorage 的本地文件系统实现。

将技能数据存储在宿主机的本地文件系统上，目录布局如下::

    <root>/public/<name>/SKILL.md
    <root>/custom/<name>/SKILL.md
    <root>/custom/.history/<name>.jsonl

设计考量
  - **简单优先**：零外部依赖，文件系统即数据库。适合单机部署和开发环境。
  - **原子写入**：``write_custom_skill()`` 使用 ``NamedTemporaryFile + os.replace``
    确保写入是原子的（不会出现写了一半的文件）。
  - **安全安装**：``ainstall_skill_from_archive()`` 通过临时目录 + 移动操作
    确保安装要么完全成功，要么不留痕迹。
  - **历史记录**：JSONL 格式（每行一个 JSON 对象），支持追加写入和按行读取，
    随着历史增长可被外部工具轻松轮转/归档。

来源映射
  本类整合了旧代码中分散在 ``loader``、``manager``、``installer`` 中的逻辑。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from deerflow.config.runtime_paths import resolve_path
from deerflow.skills.permissions import make_skill_written_path_sandbox_readable
from deerflow.skills.storage.skill_storage import SKILL_MD_FILE, SkillStorage
from deerflow.skills.types import SkillCategory

logger = logging.getLogger(__name__)

# 容器中技能的默认挂载根路径
DEFAULT_SKILLS_CONTAINER_PATH = "/mnt/skills"


class LocalSkillStorage(SkillStorage):
    """基于本地文件系统的技能存储实现。

    目录布局::

        <root>/public/<name>/SKILL.md
        <root>/custom/<name>/SKILL.md
        <root>/custom/.history/<name>.jsonl
    """

    def __init__(
        self,
        host_path: str | None = None,
        container_path: str = DEFAULT_SKILLS_CONTAINER_PATH,
        app_config=None,
    ) -> None:
        super().__init__(container_path=container_path)
        if host_path is None:
            # 未指定宿主机路径 → 从配置中读取
            from deerflow.config import get_app_config

            config = app_config or get_app_config()
            self._host_root: Path = config.skills.get_skills_path()
        else:
            # 显式指定了宿主机路径 → 直接使用（支持测试和自定义路径）
            self._host_root = resolve_path(host_path)

    # ------------------------------------------------------------------
    # 抽象操作实现
    # ------------------------------------------------------------------

    def get_skills_root_path(self) -> Path:
        return self._host_root

    def custom_skill_exists(self, name: str) -> bool:
        """检查自定义技能是否存在 —— 通过检查 SKILL.md 文件是否存在。"""
        return self.get_custom_skill_file(name).exists()

    def public_skill_exists(self, name: str) -> bool:
        """检查公共技能是否存在 —— 通过检查 SKILL.md 文件是否存在。"""
        normalized_name = self.validate_skill_name(name)
        return (self._host_root / SkillCategory.PUBLIC.value / normalized_name / SKILL_MD_FILE).exists()

    def _iter_skill_files(self) -> Iterable[tuple[SkillCategory, Path, Path]]:
        """遍历文件系统，生成所有 SKILL.md 文件的位置信息。

        使用 ``os.walk`` 而非 ``Path.rglob`` 以支持在遍历过程中
        修改 dir_names 列表（过滤掉隐藏目录）。

        Yields:
            ``(category, category_root, skill_md_path)`` 三元组。
        """
        if not self._host_root.exists():
            return
        for category in SkillCategory:
            category_path = self._host_root / category.value
            if not category_path.exists() or not category_path.is_dir():
                continue
            for current_root, dir_names, file_names in os.walk(category_path, followlinks=True):
                # 过滤隐藏目录（以 . 开头的目录），避免遍历 .history 等内部目录
                dir_names[:] = sorted(
                    name for name in dir_names if not name.startswith("."))
                if SKILL_MD_FILE not in file_names:
                    continue
                yield category, category_path, Path(current_root) / SKILL_MD_FILE

    def read_custom_skill(self, name: str) -> str:
        """读取自定义技能的 SKILL.md 完整内容。"""
        if not self.custom_skill_exists(name):
            raise FileNotFoundError(f"Custom skill '{name}' not found.")
        return (self.get_custom_skill_dir(name) / SKILL_MD_FILE).read_text(encoding="utf-8")

    def write_custom_skill(self, name: str, relative_path: str, content: str) -> None:
        """原子写入文本文件到自定义技能目录。

        使用临时文件 + ``os.replace`` 策略确保原子性：
        1. 在目标目录中创建临时文件并写入内容。
        2. 调用 ``tmp_path.replace(target)`` 原子地替换目标文件。

        这保证了读取方永远不会看到部分写入的文件。
        """
        target = self.validate_relative_path(
            relative_path, self.get_custom_skill_dir(name))
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(target.parent),
        ) as tmp_file:
            tmp_file.write(content)
            tmp_path = Path(tmp_file.name)
        # 原子替换（POSIX 保证 os.replace 是原子的）
        tmp_path.replace(target)
        make_skill_written_path_sandbox_readable(self.get_custom_skill_dir(name), target)

    async def ainstall_skill_from_archive(self, archive_path: str | Path) -> dict:
        """从 .skill ZIP 归档包安装技能。

        安装流程（全部或全不）：
        1. 校验归档文件存在且扩展名为 ``.skill``。
        2. 解压到临时目录（带安全防护：防目录穿越、防符号链接、防 zip 炸弹）。
        3. 解析归档包结构（去除 macOS 元数据等干扰文件）。
        4. 校验 frontmatter。
        5. 运行 LLM 安全扫描。
        6. 分阶段安装：先复制到 staging 临时目录，
           创建目标目录（权限 0o700），再移动文件。
           如果目标目录已存在 → 回滚。
           如果移动过程中失败 → 清理已创建的目标目录。

        Returns:
            ``{"success": True, "skill_name": ..., "message": ...}``。
        """
        import zipfile

        from deerflow.skills.installer import (
            SkillAlreadyExistsError,
            _move_staged_skill_into_reserved_target,
            _scan_skill_archive_contents_or_raise,
            resolve_skill_dir_from_archive,
            safe_extract_skill_archive,
        )
        from deerflow.skills.validation import _validate_skill_frontmatter

        logger.info("Installing skill from %s", archive_path)
        path = Path(archive_path)
        if not path.is_file():
            if not path.exists():
                raise FileNotFoundError(
                    f"Skill file not found: {archive_path}")
            raise ValueError(f"Path is not a file: {archive_path}")
        if path.suffix != ".skill":
            raise ValueError("File must have .skill extension")

        custom_dir = self._host_root / "custom"
        custom_dir.mkdir(parents=True, exist_ok=True)

        # 解压到系统临时目录
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            try:
                zf = zipfile.ZipFile(path, "r")
            except FileNotFoundError:
                raise FileNotFoundError(
                    f"Skill file not found: {archive_path}") from None
            except (zipfile.BadZipFile, IsADirectoryError):
                raise ValueError("File is not a valid ZIP archive") from None

            with zf:
                # 安全解压（防目录穿越、zip 炸弹等）
                safe_extract_skill_archive(zf, tmp_path)

            # 定位技能根目录（处理压缩包内多一层目录的情况）
            skill_dir = resolve_skill_dir_from_archive(tmp_path)

            # 校验 frontmatter
            is_valid, message, skill_name = _validate_skill_frontmatter(
                skill_dir)
            if not is_valid:
                raise ValueError(f"Invalid skill: {message}")
            if not skill_name or "/" in skill_name or "\\" in skill_name or ".." in skill_name:
                raise ValueError(f"Invalid skill name: {skill_name}")

            target = custom_dir / skill_name
            if target.exists():
                raise SkillAlreadyExistsError(
                    f"Skill '{skill_name}' already exists")

            # 安全扫描所有文件
            await _scan_skill_archive_contents_or_raise(skill_dir, skill_name)

            # 分阶段安装：staging → 预留目标 → 移动
            with tempfile.TemporaryDirectory(prefix=f".installing-{skill_name}-", dir=custom_dir) as staging_root:
                staging_target = Path(staging_root) / skill_name
                shutil.copytree(skill_dir, staging_target)
                _move_staged_skill_into_reserved_target(staging_target, target)
            logger.info("Skill %r installed to %s", skill_name, target)

        return {
            "success": True,
            "skill_name": skill_name,
            "message": f"Skill '{skill_name}' installed successfully",
        }

    def delete_custom_skill(self, name: str, *, history_meta: dict | None = None) -> None:
        """删除自定义技能，可选地保存历史记录。

        流程:
        1. 校验名称 + 确保技能可编辑。
        2. 如果提供了 ``history_meta``，保存删除前的技能内容到历史记录。
        3. 递归删除技能目录。

        历史记录写入失败（权限不足等）会被静默处理，不会阻止删除操作。
        """
        self.validate_skill_name(name)
        self.ensure_custom_skill_is_editable(name)
        target = self.get_custom_skill_dir(name)
        if history_meta is not None:
            prev_content = self.read_custom_skill(name)
            try:
                self.append_history(
                    name, {**history_meta, "prev_content": prev_content})
            except OSError as e:
                # 历史记录写入失败不阻止删除
                if not isinstance(e, PermissionError) and e.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
                    raise
                logger.warning(
                    "Skipping delete history write for custom skill %s due to readonly/permission failure; continuing with skill directory removal: %s",
                    name,
                    e,
                )
        if target.exists():
            shutil.rmtree(target)

    def append_history(self, name: str, record: dict) -> None:
        """追加 JSONL 历史记录。

        每条记录自动附带 UTC 时间戳（``ts`` 字段）。
        ``ensure_ascii=False`` 保留 Unicode 字符的可读性。
        """
        self.validate_skill_name(name)
        payload = {"ts": datetime.now(UTC).isoformat(), **record}
        history_path = self.get_skill_history_file(name)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
            f.write("\n")

    def read_history(self, name: str) -> list[dict]:
        """读取所有历史记录，按时间从旧到新排列。

        每行是一个 JSON 对象，跳过空行。
        """
        self.validate_skill_name(name)
        history_path = self.get_skill_history_file(name)
        if not history_path.exists():
            return []
        records: list[dict] = []
        for line in history_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            records.append(json.loads(line))
        return records
