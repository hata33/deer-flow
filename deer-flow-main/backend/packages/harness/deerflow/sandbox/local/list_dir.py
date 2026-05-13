"""目录列表工具。

递归列出目录内容（树形格式），自动忽略版本控制、依赖、构建输出、IDE 文件等。
"""

import fnmatch
from pathlib import Path

# 忽略的文件/目录模式
IGNORE_PATTERNS = [
    # Version Control
    ".git",
    ".svn",
    ".hg",
    ".bzr",
    # Dependencies
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".env",
    "env",
    ".tox",
    ".nox",
    ".eggs",
    "*.egg-info",
    "site-packages",
    # Build outputs
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".output",
    ".turbo",
    "target",
    "out",
    # IDE & Editor
    ".idea",
    ".vscode",
    "*.swp",
    "*.swo",
    "*~",
    ".project",
    ".classpath",
    ".settings",
    # OS generated
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "*.lnk",
    # Logs & temp files
    "*.log",
    "*.tmp",
    "*.temp",
    "*.bak",
    "*.cache",
    ".cache",
    "logs",
    # Coverage & test artifacts
    ".coverage",
    "coverage",
    ".nyc_output",
    "htmlcov",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
]


def _should_ignore(name: str) -> bool:
    """检查文件/目录名是否匹配忽略模式。"""
    for pattern in IGNORE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def list_dir(path: str, max_depth: int = 2) -> list[str]:
    """递归列出目录内容（最多 max_depth 层），自动忽略无关文件。

    Args:
        path: 根目录路径。
        max_depth: 最大遍历深度（1=直接子项，2=子项+孙项）。

    Returns:
        排序后的绝对路径列表，目录以 / 结尾。
    """
    result: list[str] = []
    root_path = Path(path).resolve()

    if not root_path.is_dir():
        return result

    def _traverse(current_path: Path, current_depth: int) -> None:
        """递归遍历目录直到 max_depth。"""
        if current_depth > max_depth:
            return

        try:
            for item in current_path.iterdir():
                if _should_ignore(item.name):
                    continue

                post_fix = "/" if item.is_dir() else ""
                result.append(str(item.resolve()) + post_fix)

                if item.is_dir() and current_depth < max_depth:
                    _traverse(item, current_depth + 1)
        except PermissionError:
            pass

    _traverse(root_path, 1)

    return sorted(result)
