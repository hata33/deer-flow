"""
内置工具

提供常用的文件操作和命令执行工具
"""
import logging
from typing import Optional

from langchain_core.tools import StructuredTool

from tools.registry import get_tool_registry, tool

logger = logging.getLogger(__name__)


# ============================================================================
# Bash 工具
# ============================================================================

@tool(
    name="bash",
    description="执行 bash 命令。用于运行 shell 命令、脚本和程序。"
)
def bash_tool(command: str) -> str:
    """
    执行 bash 命令

    Args:
        command: 要执行的命令

    Returns:
        命令输出
    """
    import subprocess

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"

        if result.returncode != 0:
            output += f"\n[退出码: {result.returncode}]"

        return output

    except subprocess.TimeoutExpired:
        return "命令执行超时"
    except Exception as e:
        return f"命令执行错误: {e}"


# ============================================================================
# 文件读取工具
# ============================================================================

@tool(
    name="read_file",
    description="读取文件内容。支持指定行号范围。"
)
def read_file_tool(
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    """
    读取文件内容

    Args:
        path: 文件路径
        start_line: 起始行号（从1开始）
        end_line: 结束行号

    Returns:
        文件内容
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

        if start_line is not None:
            start = max(0, start_line - 1)
            end = len(lines) if end_line is None else min(end_line, len(lines))
            lines = lines[start:end]

        return "".join(lines)

    except FileNotFoundError:
        return f"文件不存在: {path}"
    except Exception as e:
        return f"读取文件错误: {e}"


# ============================================================================
# 文件写入工具
# ============================================================================

@tool(
    name="write_file",
    description="写入内容到文件。如果文件不存在则创建，如果存在则覆盖。"
)
def write_file_tool(
    path: str,
    content: str,
    append: bool = False,
) -> str:
    """
    写入文件

    Args:
        path: 文件路径
        content: 文件内容
        append: 是否追加模式

    Returns:
        操作结果
    """
    try:
        # 确保目录存在
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

        mode = "a" if append else "w"
        with open(path, mode, encoding="utf-8") as f:
            f.write(content)

        action = "追加" if append else "写入"
        return f"成功{action}文件: {path}"

    except Exception as e:
        return f"写入文件错误: {e}"


# ============================================================================
# 目录列表工具
# ============================================================================

@tool(
    name="list_dir",
    description="列出目录内容。返回文件和子目录列表。"
)
def list_dir_tool(path: str = ".", max_depth: int = 2) -> str:
    """
    列出目录内容

    Args:
        path: 目录路径
        max_depth: 最大遍历深度

    Returns:
        目录内容列表
    """
    from pathlib import Path

    try:
        dir_path = Path(path)

        if not dir_path.exists():
            return f"目录不存在: {path}"

        if not dir_path.is_dir():
            return f"不是目录: {path}"

        result = []

        def _list_recursive(current_path: Path, depth: int = 0):
            if depth > max_depth:
                return

            try:
                for item in current_path.iterdir():
                    relative = item.relative_to(dir_path)
                    marker = "/" if item.is_dir() else ""
                    result.append(f"{relative}{marker}")

                    if item.is_dir() and depth < max_depth:
                        _list_recursive(item, depth + 1)
            except PermissionError:
                pass

        _list_recursive(dir_path)
        return "\n".join(sorted(result))

    except Exception as e:
        return f"列出目录错误: {e}"


# ============================================================================
# 初始化内置工具
# ============================================================================

# 装饰器会在函数定义时自动注册工具
# 不需要手动初始化，避免循环导入问题
