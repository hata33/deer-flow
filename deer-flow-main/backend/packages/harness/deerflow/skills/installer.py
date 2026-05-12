"""技能归档安装逻辑。

纯业务逻辑——无 FastAPI/HTTP 依赖。
Gateway 和 Client 共用此模块进行 .skill 归档的安装。

安全防护：
- 拒绝绝对路径和目录遍历（..）的压缩成员
- 跳过符号链接成员（不实际化）
- 压缩炸弹防御（总解压大小限制）
- 安装前 frontmatter 校验
- 同名技能拒绝覆盖
"""

import logging
import posixpath
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from deerflow.skills.loader import get_skills_root_path
from deerflow.skills.validation import _validate_skill_frontmatter

logger = logging.getLogger(__name__)


class SkillAlreadyExistsError(ValueError):
    """同名技能已安装时抛出。"""


def is_unsafe_zip_member(info: zipfile.ZipInfo) -> bool:
    """检查压缩成员路径是否不安全（绝对路径或目录遍历）。

    同时检查 POSIX 和 Windows 风格的绝对路径，防止跨平台绕过。

    Args:
        info: ZIP 文件成员信息。

    Returns:
        路径不安全时返回 True。
    """
    name = info.filename
    if not name:
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return True
    if PureWindowsPath(name).is_absolute():
        return True
    if ".." in path.parts:
        return True
    return False


def is_symlink_member(info: zipfile.ZipInfo) -> bool:
    """检测 ZIP 成员是否为符号链接（基于 external_attr 中的 Unix 模式位）。

    Args:
        info: ZIP 文件成员信息。

    Returns:
        是符号链接时返回 True。
    """
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def should_ignore_archive_entry(path: Path) -> bool:
    """判断归档条目是否应被忽略（macOS 元数据和隐藏文件）。

    Args:
        path: 文件路径。

    Returns:
        应忽略时返回 True。
    """
    return path.name.startswith(".") or path.name == "__MACOSX"


def resolve_skill_dir_from_archive(temp_path: Path) -> Path:
    """从解压后的归档内容中定位技能根目录。

    过滤 macOS 元数据（__MACOSX）和隐藏文件后，
    若仅剩一个子目录则以此为根，否则认为当前目录即为根。

    Args:
        temp_path: 归档解压的临时目录路径。

    Returns:
        技能根目录路径。

    Raises:
        ValueError: 归档过滤后为空。
    """
    items = [p for p in temp_path.iterdir() if not should_ignore_archive_entry(p)]
    if not items:
        raise ValueError("Skill archive is empty")
    if len(items) == 1 and items[0].is_dir():
        return items[0]
    return temp_path


def safe_extract_skill_archive(
    zip_ref: zipfile.ZipFile,
    dest_path: Path,
    max_total_size: int = 512 * 1024 * 1024,
) -> None:
    """安全解压技能归档，包含多层安全防护。

    防护措施：
    - 拒绝绝对路径和目录遍历（..）成员
    - 跳过符号链接成员（不实际化）
    - 解压后路径二次校验（防止绕过）
    - 硬性总大小限制（zip bomb 防御，默认 512MB）

    Args:
        zip_ref: 已打开的 ZIP 文件对象。
        dest_path: 解压目标目录。
        max_total_size: 最大解压总大小（字节）。

    Raises:
        ValueError: 存在不安全成员或超出大小限制。
    """
    dest_root = dest_path.resolve()
    total_written = 0

    for info in zip_ref.infolist():
        # 第一层：检查路径安全性
        if is_unsafe_zip_member(info):
            raise ValueError(f"Archive contains unsafe member path: {info.filename!r}")

        # 跳过符号链接
        if is_symlink_member(info):
            logger.warning("Skipping symlink entry in skill archive: %s", info.filename)
            continue

        # 第二层：规范化路径并验证不越界
        normalized_name = posixpath.normpath(info.filename.replace("\\", "/"))
        member_path = dest_root.joinpath(*PurePosixPath(normalized_name).parts)
        if not member_path.resolve().is_relative_to(dest_root):
            raise ValueError(f"Zip entry escapes destination: {info.filename!r}")
        member_path.parent.mkdir(parents=True, exist_ok=True)

        if info.is_dir():
            member_path.mkdir(parents=True, exist_ok=True)
            continue

        # 第三层：逐块写入并累计大小（zip bomb 防御）
        with zip_ref.open(info) as src, member_path.open("wb") as dst:
            while chunk := src.read(65536):
                total_written += len(chunk)
                if total_written > max_total_size:
                    raise ValueError("Skill archive is too large or appears highly compressed.")
                dst.write(chunk)


def install_skill_from_archive(
    zip_path: str | Path,
    *,
    skills_root: Path | None = None,
) -> dict:
    """从 .skill 归档文件安装技能。

    安装流程：
    1. 校验文件存在性和扩展名（.skill）
    2. 在临时目录中安全解压
    3. 定位技能根目录并校验 frontmatter
    4. 检查同名冲突
    5. 复制到 custom/ 目录

    Args:
        zip_path: .skill 归档文件路径。
        skills_root: 自定义技能根目录，为 None 时使用默认路径。

    Returns:
        包含 success、skill_name、message 的字典。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件无效（扩展名、ZIP 格式、frontmatter、同名冲突）。
        SkillAlreadyExistsError: 同名技能已安装。
    """
    logger.info("Installing skill from %s", zip_path)
    path = Path(zip_path)
    if not path.is_file():
        if not path.exists():
            raise FileNotFoundError(f"Skill file not found: {zip_path}")
        raise ValueError(f"Path is not a file: {zip_path}")
    if path.suffix != ".skill":
        raise ValueError("File must have .skill extension")

    if skills_root is None:
        skills_root = get_skills_root_path()
    custom_dir = skills_root / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)

    # 在临时目录中解压并校验
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        try:
            zf = zipfile.ZipFile(path, "r")
        except FileNotFoundError:
            raise FileNotFoundError(f"Skill file not found: {zip_path}") from None
        except (zipfile.BadZipFile, IsADirectoryError):
            raise ValueError("File is not a valid ZIP archive") from None

        with zf:
            safe_extract_skill_archive(zf, tmp_path)

        # 定位技能根目录
        skill_dir = resolve_skill_dir_from_archive(tmp_path)

        # 校验 frontmatter 格式和内容
        is_valid, message, skill_name = _validate_skill_frontmatter(skill_dir)
        if not is_valid:
            raise ValueError(f"Invalid skill: {message}")
        # 二次校验技能名称安全性（防止路径注入）
        if not skill_name or "/" in skill_name or "\\" in skill_name or ".." in skill_name:
            raise ValueError(f"Invalid skill name: {skill_name}")

        # 检查同名冲突（不允许覆盖）
        target = custom_dir / skill_name
        if target.exists():
            raise SkillAlreadyExistsError(f"Skill '{skill_name}' already exists")

        # 复制到 custom 目录
        shutil.copytree(skill_dir, target)
        logger.info("Skill %r installed to %s", skill_name, target)

    return {
        "success": True,
        "skill_name": skill_name,
        "message": f"Skill '{skill_name}' installed successfully",
    }
