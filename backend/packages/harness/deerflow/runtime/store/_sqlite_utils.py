"""存储和检查点提供者的共享 SQLite 连接工具模块。"""

from __future__ import annotations

import pathlib

from deerflow.config.paths import resolve_path


def resolve_sqlite_conn_str(raw: str) -> str:
    """返回一个可用于存储/检查点后端的 SQLite 连接字符串。

    SQLite 特殊字符串（``":memory:"`` 和 ``file:`` URI）原样返回。
    普通文件系统路径（相对或绝对）通过 :func:`resolve_path` 解析为绝对路径字符串。

    Args:
        raw: 原始连接字符串

    Returns:
        解析后的 SQLite 连接字符串
    """
    if raw == ":memory:" or raw.startswith("file:"):
        return raw
    return str(resolve_path(raw))


def ensure_sqlite_parent_dir(conn_str: str) -> None:
    """为 SQLite 文件系统路径创建父目录。

    对于内存数据库（``":memory:"``）和 ``file:`` URI 为空操作。

    Args:
        conn_str: SQLite 连接字符串
    """
    if conn_str != ":memory:" and not conn_str.startswith("file:"):
        pathlib.Path(conn_str).parent.mkdir(parents=True, exist_ok=True)

