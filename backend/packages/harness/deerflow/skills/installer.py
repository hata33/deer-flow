"""技能归档包（.skill）安装的共享逻辑。

纯业务逻辑 —— 不依赖 FastAPI/HTTP。Gateway 和 Client 都委托给这些函数。

安全模型
  技能归档包是来自外部的不可信 ZIP 文件。安装过程实施多层防护：
  1. **路径安全**：拒绝绝对路径、目录穿越（``..``）、符号链接。
  2. **解压炸弹防护**：限制总解压大小（默认 512 MB）。
  3. **类型过滤**：跳过 macOS 元数据（``__MACOSX``）和隐藏文件。
  4. **LLM 安全审查**：所有文本和脚本文件经过 ``scan_skill_content`` 审查。
  5. **原子安装**：使用 staging → 预留目录 → 移动的三阶段提交，
     确保安装要么完全成功，要么不留痕迹。

异步处理
  安装流程包含异步安全扫描。当从同步上下文调用时，
  ``_run_async_install`` 处理事件循环检测并在必要时创建临时事件循环。
"""

import asyncio
import concurrent.futures
import logging
import posixpath
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from deerflow.skills.security_scanner import scan_skill_content

logger = logging.getLogger(__name__)

# 安全扫描中视为 prompt 输入的目录和文件后缀
_PROMPT_INPUT_DIRS = {"references", "templates"}
_PROMPT_INPUT_SUFFIXES = frozenset(
    {".json", ".markdown", ".md", ".rst", ".txt", ".yaml", ".yml"})


class SkillAlreadyExistsError(ValueError):
    """同名技能已安装时抛出。"""


class SkillSecurityScanError(ValueError):
    """技能归档包未通过安全扫描时抛出。"""


def is_unsafe_zip_member(info: zipfile.ZipInfo) -> bool:
    """检查 ZIP 条目路径是否为绝对路径或尝试目录穿越。

    同时检查 POSIX 路径（``/``）和 Windows 路径（``C:\\``）两种绝对路径格式，
    以及路径中的 ``..`` 组件。使用 ``PurePosixPath`` 和 ``PureWindowsPath``
    而非正则表达式，以正确处理边界情况。
    """
    name = info.filename
    if not name:
        return False
    # 统一路径分隔符为 /
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return True
    # 同时检查 Windows 绝对路径（如 C:\foo\bar）
    if PureWindowsPath(name).is_absolute():
        return True
    if ".." in path.parts:
        return True
    return False


def is_symlink_member(info: zipfile.ZipInfo) -> bool:
    """根据 ZipInfo 中存储的 external_attr 检测符号链接。

    ZIP 格式通过 external_attr 的高 16 位存储 Unix 文件模式。
    ``stat.S_ISLNK`` 检查该模式是否为符号链接。
    """
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def should_ignore_archive_entry(path: Path) -> bool:
    """判断是否应忽略归档包中的条目（macOS 元数据和隐藏文件）。"""
    return path.name.startswith(".") or path.name == "__MACOSX"


def resolve_skill_dir_from_archive(temp_path: Path) -> Path:
    """从解压后的归档包内容中定位技能根目录。

    过滤掉 macOS 元数据（__MACOSX）和隐藏文件（.DS_Store）。
    如果过滤后只剩一个目录，假设压缩包内多了一层包装目录，
    返回该内部目录；否则返回临时路径本身。

    Returns:
        技能目录的路径。

    Raises:
        ValueError: 过滤后归档包为空。
    """
    items = [p for p in temp_path.iterdir(
    ) if not should_ignore_archive_entry(p)]
    if not items:
        raise ValueError("Skill archive is empty")
    # 如果只有一个条目且是目录 → 压缩包内多了一层包装
    if len(items) == 1 and items[0].is_dir():
        return items[0]
    return temp_path


def safe_extract_skill_archive(
    zip_ref: zipfile.ZipFile,
    dest_path: Path,
    max_total_size: int = 512 * 1024 * 1024,
) -> None:
    """安全解压技能归档包，带多层安全防护。

    防护措施:
    - 拒绝绝对路径和目录穿越（``..``）。
    - 跳过符号链接条目（不物化）。
    - 强制限制总解压大小（zip 炸弹防御，默认 512 MB）。
    - 再次检查解析后的路径是否在目标目录内（双重保险）。

    Raises:
        ValueError: 检测到不安全的条目或超出大小限制。
    """
    dest_root = dest_path.resolve()
    total_written = 0

    for info in zip_ref.infolist():
        # 安全检查 1：路径安全性
        if is_unsafe_zip_member(info):
            raise ValueError(
                f"Archive contains unsafe member path: {info.filename!r}")

        # 安全检查 2：跳过符号链接
        if is_symlink_member(info):
            logger.warning(
                "Skipping symlink entry in skill archive: %s", info.filename)
            continue

        # 规范化路径并解析为绝对路径
        normalized_name = posixpath.normpath(info.filename.replace("\\", "/"))
        member_path = dest_root.joinpath(*PurePosixPath(normalized_name).parts)
        # 安全检查 3：二次确认路径在目标目录内
        if not member_path.resolve().is_relative_to(dest_root):
            raise ValueError(
                f"Zip entry escapes destination: {info.filename!r}")
        member_path.parent.mkdir(parents=True, exist_ok=True)

        if info.is_dir():
            member_path.mkdir(parents=True, exist_ok=True)
            continue

        # 按块读取并累积大小，防止 zip 炸弹
        with zip_ref.open(info) as src, member_path.open("wb") as dst:
            while chunk := src.read(65536):
                total_written += len(chunk)
                if total_written > max_total_size:
                    raise ValueError(
                        "Skill archive is too large or appears highly compressed.")
                dst.write(chunk)


def _is_script_support_file(rel_path: Path) -> bool:
    """判断辅助文件是否在 scripts/ 目录下（视为可执行文件进行更严格审查）。"""
    return bool(rel_path.parts) and rel_path.parts[0] == "scripts"


def _should_scan_support_file(rel_path: Path) -> bool:
    """判断辅助文件是否需要进行安全扫描。

    扫描规则：
    - scripts/ 下的所有文件（可执行文件 → 更严格审查）。
    - references/ 和 templates/ 下的文本类文件（.json, .md, .txt, .yaml 等）。
    """
    if _is_script_support_file(rel_path):
        return True
    return bool(rel_path.parts) and rel_path.parts[0] in _PROMPT_INPUT_DIRS and rel_path.suffix.lower() in _PROMPT_INPUT_SUFFIXES


def _move_staged_skill_into_reserved_target(staging_target: Path, target: Path) -> None:
    """将 staging 目录中的技能文件移动到预留的目标目录。

    实现原子安装的三阶段提交：
    1. 创建目标目录（权限 0o700 —— 仅所有者可读写执行）。
    2. 将 staging 中的文件逐个移动到目标目录。
    3. 如果任何步骤失败，清理已创建的目标目录。

    这样确保目标目录要么完全填充，要么完全不存在。

    Raises:
        SkillAlreadyExistsError: 目标目录已存在（并发安装检测）。
    """
    installed = False
    reserved = False
    try:
        target.mkdir(mode=0o700)
        reserved = True
        for child in staging_target.iterdir():
            shutil.move(str(child), target / child.name)
        installed = True
    except FileExistsError as e:
        raise SkillAlreadyExistsError(
            f"Skill '{target.name}' already exists") from e
    finally:
        # 回滚：如果预留了目录但安装未完成 → 清理
        if reserved and not installed and target.exists():
            shutil.rmtree(target)


async def _scan_skill_file_or_raise(skill_dir: Path, path: Path, skill_name: str, *, executable: bool) -> None:
    """扫描单个技能文件的安全性，不通过则抛出异常。

    文件必须为有效 UTF-8 编码。二进制文件或非 UTF-8 编码会触发扫描失败。

    Args:
        skill_dir: 技能根目录。
        path: 待扫描文件的绝对路径。
        skill_name: 技能名称（用于错误消息）。
        executable: 是否为可执行文件（脚本类的审查更严格）。

    Raises:
        SkillSecurityScanError: 安全扫描失败或文件被判定为 block。
    """
    rel_path = path.relative_to(skill_dir).as_posix()
    location = f"{skill_name}/{rel_path}"
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise SkillSecurityScanError(
            f"Security scan failed for skill '{skill_name}': {location} must be valid UTF-8") from e

    try:
        result = await scan_skill_content(content, executable=executable, location=location)
    except Exception as e:
        raise SkillSecurityScanError(
            f"Security scan failed for {location}: {e}") from e

    decision = getattr(result, "decision", None)
    reason = str(getattr(result, "reason", "") or "No reason provided.")
    if decision == "block":
        # SKILL.md 被 block → 直接拒绝整个技能
        if rel_path == "SKILL.md":
            raise SkillSecurityScanError(
                f"Security scan blocked skill '{skill_name}': {reason}")
        raise SkillSecurityScanError(
            f"Security scan blocked {location}: {reason}")
    # 可执行文件至少需要 allow 判定（warn 也不行）
    if executable and decision != "allow":
        raise SkillSecurityScanError(
            f"Security scan rejected executable {location}: {reason}")
    # 未知判定 → 保守拒绝
    if decision not in {"allow", "warn"}:
        raise SkillSecurityScanError(
            f"Security scan failed for {location}: invalid scanner decision {decision!r}")


async def _scan_skill_archive_contents_or_raise(skill_dir: Path, skill_name: str) -> None:
    """对技能目录中所有可安装的文本和脚本文件运行安全扫描。

    扫描流程:
    1. 首先扫描 SKILL.md（最重要的文件）。
    2. 遍历所有辅助文件，根据路径和类型决定是否扫描。
    3. 检测并拒绝嵌套的 SKILL.md 文件（安全策略：不允许技能内嵌套技能）。
    """
    skill_md = skill_dir / "SKILL.md"
    await _scan_skill_file_or_raise(skill_dir, skill_md, skill_name, executable=False)

    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue

        rel_path = path.relative_to(skill_dir)
        if rel_path == Path("SKILL.md"):
            continue
        # 不允许嵌套的 SKILL.md
        if path.name == "SKILL.md":
            raise SkillSecurityScanError(
                f"Security scan failed for skill '{skill_name}': nested SKILL.md is not allowed at {skill_name}/{rel_path.as_posix()}")
        if not _should_scan_support_file(rel_path):
            continue

        await _scan_skill_file_or_raise(skill_dir, path, skill_name, executable=_is_script_support_file(rel_path))


def _run_async_install(coro):
    """在同步上下文中运行异步安装协程。

    处理两种情况：
    1. 当前有运行中的事件循环 → 在单独的线程中创建新事件循环执行。
       这避免了与主事件循环的冲突。
    2. 当前没有事件循环 → 直接运行 ``asyncio.run()``。

    这个函数被 ``SkillStorage.install_skill_from_archive()`` 同步包装器调用。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # 有运行中的事件循环 → 在独立线程中执行，避免冲突
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coro).result()
    return asyncio.run(coro)
