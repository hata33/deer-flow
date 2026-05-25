"""文件搜索工具 —— 提供 glob 模式匹配和 grep 内容搜索功能。

本模块实现了沙箱子系统中的文件搜索能力，包括两个核心功能：

1. **Glob 搜索** (:func:`find_glob_matches`)：根据 glob 模式在目录树中查找
   匹配的文件和目录路径。支持 ``*``、``**``、``?`` 等标准通配符。
2. **Grep 搜索** (:func:`find_grep_matches`)：在文本文件中搜索匹配指定正则
   表达式或字面量的行，返回匹配的文件路径、行号和行内容。

忽略模式
~~~~~~~~
搜索过程会自动跳过常见的无关目录和文件，包括：

- 版本控制目录：``.git``、``.svn``、``.hg``、``.bzr``
- 依赖目录：``node_modules``、``__pycache__``、``.venv``、``site-packages``
- 构建产物：``dist``、``build``、``.next``、``target``、``out``
- IDE 配置：``.idea``、``.vscode``
- 临时文件：``*.tmp``、``*.temp``、``*.bak``、``*.cache``
- 系统文件：``.DS_Store``、``Thumbs.db``

安全限制
~~~~~~~~
- 文件大小超过 ``DEFAULT_MAX_FILE_SIZE_BYTES``（1MB）的文件不会被 grep 搜索
- 二进制文件（通过 ``\\0`` 字节检测）会被自动跳过
- 超长行（超过行摘要长度的 10 倍）会被跳过以防止 ReDoS 攻击
- 结果数量有上限（glob 默认 200 条，grep 默认 100 条），超出时截断并标记
"""

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# 搜索时需要忽略的目录名和文件名模式列表。
# 这些模式使用 fnmatch 语法（支持 * 和 ? 通配符）。
# 目录遍历时，匹配这些模式的名称将被跳过，不进入搜索。
IGNORE_PATTERNS = [
    ".git",           # Git 版本控制目录
    ".svn",           # Subversion 版本控制目录
    ".hg",            # Mercurial 版本控制目录
    ".bzr",           # Bazaar 版本控制目录
    "node_modules",   # Node.js 依赖目录
    "__pycache__",    # Python 字节码缓存目录
    ".venv",          # Python 虚拟环境目录
    "venv",           # Python 虚拟环境目录（无点前缀）
    ".env",           # 环境变量文件 / 目录
    "env",            # 环境变量文件 / 目录（无点前缀）
    ".tox",           # Tox 测试环境目录
    ".nox",           # Nox 测试环境目录
    ".eggs",          # Python eggs 目录
    "*.egg-info",     # Python egg 信息目录
    "site-packages",  # Python 第三方包安装目录
    "dist",           # 构建产物目录
    "build",          # 构建产物目录
    ".next",          # Next.js 构建目录
    ".nuxt",          # Nuxt.js 构建目录
    ".output",        # Nuxt 输出目录
    ".turbo",         # Turborepo 缓存目录
    "target",         # Maven / Gradle 构建目标目录
    "out",            # 通用构建输出目录
    ".idea",          # JetBrains IDE 配置目录
    ".vscode",        # VS Code 配置目录
    "*.swp",          # Vim 交换文件
    "*.swo",          # Vim 交换文件
    "*~",             # 备份文件（编辑器生成）
    ".project",       # Eclipse 项目文件
    ".classpath",     # Eclipse 类路径文件
    ".settings",      # Eclipse 设置目录
    ".DS_Store",      # macOS 目录元数据文件
    "Thumbs.db",      # Windows 缩略图缓存
    "desktop.ini",    # Windows 桌面配置文件
    "*.lnk",          # Windows 快捷方式文件
    "*.log",          # 日志文件
    "*.tmp",          # 临时文件
    "*.temp",         # 临时文件
    "*.bak",          # 备份文件
    "*.cache",        # 缓存文件
    ".cache",         # 缓存目录
    "logs",           # 日志目录
    ".coverage",      # Python 覆盖率数据文件
    "coverage",       # 覆盖率报告目录
    ".nyc_output",    # NYC 覆盖率输出目录
    "htmlcov",        # HTML 覆盖率报告目录
    ".pytest_cache",  # pytest 缓存目录
    ".mypy_cache",    # mypy 类型检查缓存目录
    ".ruff_cache",    # Ruff Linter 缓存目录
]

# grep 搜索时跳过超过此大小的文件（默认 1MB），避免读取过大文件导致性能问题
DEFAULT_MAX_FILE_SIZE_BYTES = 1_000_000

# grep 匹配行的最大显示长度（默认 200 字符），超长行会被截断并添加 "..." 后缀
DEFAULT_LINE_SUMMARY_LENGTH = 200


@dataclass(frozen=True)
class GrepMatch:
    """grep 搜索的单行匹配结果。

    Attributes:
        path: 匹配文件的绝对路径（经过 reverse resolve 后的虚拟路径）。
        line_number: 匹配行在文件中的行号（从 1 开始）。
        line: 匹配行的文本内容（可能被截断至 DEFAULT_LINE_SUMMARY_LENGTH）。
    """
    path: str
    line_number: int
    line: str


def should_ignore_name(name: str) -> bool:
    """判断给定文件名或目录名是否应被搜索忽略。

    逐个检查 IGNORE_PATTERNS 中的 fnmatch 模式，只要有一个匹配则返回 True。

    Args:
        name: 文件名或目录名（不含路径前缀）。

    Returns:
        如果名称匹配任何忽略模式则返回 True，否则返回 False。
    """
    for pattern in IGNORE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def should_ignore_path(path: str) -> bool:
    """判断路径中的任何路径段是否匹配忽略模式。

    将路径按 ``/`` 分隔为多个段，逐一检查每段是否匹配 IGNORE_PATTERNS。

    Args:
        path: 文件路径（支持 Windows 反斜杠，会自动转换为正斜杠）。

    Returns:
        如果路径中任一段匹配忽略模式则返回 True。
    """
    return any(should_ignore_name(segment) for segment in path.replace("\\", "/").split("/") if segment)


def path_matches(pattern: str, rel_path: str) -> bool:
    """判断相对路径是否匹配 glob 模式。

    使用 :class:`PurePosixPath.match` 进行匹配，并额外支持以 ``**/`` 开头的
    模式（在任意深度匹配）。

    Args:
        pattern: glob 模式字符串（如 ``"*.py"`` 或 ``"**/*.json"``）。
        rel_path: 相对于搜索根目录的路径（POSIX 格式）。

    Returns:
        如果路径匹配模式则返回 True。
    """
    path = PurePosixPath(rel_path)
    # 先尝试直接匹配
    if path.match(pattern):
        return True
    # 对于以 **/ 开头的模式，去掉前缀后再尝试匹配
    # 例如 **/*.json 应匹配 src/foo.json
    if pattern.startswith("**/"):
        return path.match(pattern[3:])
    return False


def truncate_line(line: str, max_chars: int = DEFAULT_LINE_SUMMARY_LENGTH) -> str:
    """截断过长的行内容，用于 grep 结果的摘要显示。

    先去除行尾换行符，再检查长度。如果超过 max_chars，则截断并添加 ``...`` 后缀。

    Args:
        line: 原始行文本。
        max_chars: 最大字符数（默认 200）。

    Returns:
        截断后的行文本。
    """
    line = line.rstrip("\n\r")
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 3] + "..."


def is_binary_file(path: Path, sample_size: int = 8192) -> bool:
    """检测文件是否为二进制文件。

    通过读取文件前 ``sample_size`` 个字节，检查是否包含空字节（``\\0``）。
    如果包含则判定为二进制文件。如果读取失败（权限不足等），也视为二进制文件。

    Args:
        path: 文件路径。
        sample_size: 采样字节数（默认 8KB）。

    Returns:
        如果是二进制文件（或无法读取）则返回 True。
    """
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(sample_size)
    except OSError:
        # 无法读取的文件视为二进制文件，跳过搜索
        return True


def find_glob_matches(root: Path, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
    """在目录树中查找匹配 glob 模式的文件和目录路径。

    从 ``root`` 开始递归遍历目录树（忽略 IGNORE_PATTERNS 匹配的目录），
    对每个文件和（可选的）目录名进行 glob 模式匹配。

    Args:
        root: 搜索的根目录路径。
        pattern: glob 模式字符串。
        include_dirs: 是否在结果中包含目录（默认仅包含文件）。
        max_results: 最大返回结果数（默认 200）。

    Returns:
        ``(matches, truncated)`` 元组：
        - matches: 匹配的绝对路径列表。
        - truncated: 如果结果因达到上限被截断则为 True。

    Raises:
        FileNotFoundError: 如果 root 不存在。
        NotADirectoryError: 如果 root 不是目录。
    """
    matches: list[str] = []
    truncated = False
    root = root.resolve()  # 解析为绝对路径

    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    for current_root, dirs, files in os.walk(root):
        # 过滤掉匹配忽略模式的子目录，os.walk 不会再进入这些目录
        dirs[:] = [name for name in dirs if not should_ignore_name(name)]
        # root 已被 resolve()，os.walk 通过 join root 构造 current_root，
        # 所以 relative_to() 不需要额外的 stat()/resolve() 调用。
        rel_dir = Path(current_root).relative_to(root)

        # 如果需要包含目录，先处理目录匹配
        if include_dirs:
            for name in dirs:
                rel_path = (rel_dir / name).as_posix()
                if path_matches(pattern, rel_path):
                    matches.append(str(Path(current_root) / name))
                    if len(matches) >= max_results:
                        truncated = True
                        return matches, truncated

        # 处理文件匹配
        for name in files:
            if should_ignore_name(name):
                continue
            rel_path = (rel_dir / name).as_posix()
            if path_matches(pattern, rel_path):
                matches.append(str(Path(current_root) / name))
                if len(matches) >= max_results:
                    truncated = True
                    return matches, truncated

    return matches, truncated


def find_grep_matches(
    root: Path,
    pattern: str,
    *,
    glob_pattern: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    max_results: int = 100,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    line_summary_length: int = DEFAULT_LINE_SUMMARY_LENGTH,
) -> tuple[list[GrepMatch], bool]:
    """在目录树的文本文件中搜索匹配指定模式的行。

    递归遍历 ``root`` 下的所有文件，对每个非二进制、大小不超过限制的文本文件
    逐行搜索正则表达式匹配。

    Args:
        root: 搜索的根目录路径。
        pattern: 搜索模式（正则表达式或字面量文本）。
        glob_pattern: 可选的 glob 模式，仅搜索文件名匹配此模式的文件。
        literal: 如果为 True，将 pattern 视为字面量文本（自动转义正则特殊字符）。
        case_sensitive: 是否区分大小写（默认不区分）。
        max_results: 最大返回匹配数（默认 100）。
        max_file_size: 跳过超过此大小的文件（默认 1MB）。
        line_summary_length: 匹配行的最大显示长度（默认 200 字符）。

    Returns:
        ``(matches, truncated)`` 元组：
        - matches: :class:`GrepMatch` 对象列表。
        - truncated: 如果结果因达到上限被截断则为 True。

    Raises:
        FileNotFoundError: 如果 root 不存在。
        NotADirectoryError: 如果 root 不是目录。
    """
    matches: list[GrepMatch] = []
    truncated = False
    root = root.resolve()

    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    # 编译正则表达式：literal 模式下转义特殊字符
    regex_source = re.escape(pattern) if literal else pattern
    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(regex_source, flags)

    # 跳过超过此长度的行，防止在压缩/无换行的文件上触发 ReDoS（正则表达式拒绝服务攻击）
    _max_line_chars = line_summary_length * 10

    for current_root, dirs, files in os.walk(root):
        # 过滤掉匹配忽略模式的子目录
        dirs[:] = [name for name in dirs if not should_ignore_name(name)]
        rel_dir = Path(current_root).relative_to(root)

        for name in files:
            if should_ignore_name(name):
                continue

            candidate_path = Path(current_root) / name
            rel_path = (rel_dir / name).as_posix()

            # 如果指定了 glob 模式，先检查文件名是否匹配
            if glob_pattern is not None and not path_matches(glob_pattern, rel_path):
                continue

            try:
                # 安全检查：跳过符号链接，防止目录遍历
                if candidate_path.is_symlink():
                    continue
                # 解析真实路径，确保不逃逸出 root 目录
                file_path = candidate_path.resolve()
                if not file_path.is_relative_to(root):
                    continue
                # 跳过过大文件和二进制文件
                if file_path.stat().st_size > max_file_size or is_binary_file(file_path):
                    continue
                # 逐行搜索匹配
                with file_path.open(encoding="utf-8", errors="replace") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        # 跳过超长行，防止 ReDoS
                        if len(line) > _max_line_chars:
                            continue
                        if regex.search(line):
                            matches.append(
                                GrepMatch(
                                    path=str(file_path),
                                    line_number=line_number,
                                    line=truncate_line(line, line_summary_length),
                                )
                            )
                            if len(matches) >= max_results:
                                truncated = True
                                return matches, truncated
            except OSError:
                # 文件无法读取（权限不足等），静默跳过
                continue

    return matches, truncated
