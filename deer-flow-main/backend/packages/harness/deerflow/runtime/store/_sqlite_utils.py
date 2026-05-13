"""SQLite 连接工具函数。

供 Store 和 Checkpointer 提供者共享，处理路径解析和目录创建。
"""

from __future__ import annotations

import pathlib

from deerflow.config.paths import resolve_path


def resolve_sqlite_conn_str(raw: str) -> str:
    """解析 SQLite 连接字符串。

    特殊字符串（":memory:" 和 "file:" URI）原样返回，
    普通文件系统路径通过 resolve_path 转为绝对路径。
    """
    if raw == ":memory:" or raw.startswith("file:"):
        return raw
    return str(resolve_path(raw))


def ensure_sqlite_parent_dir(conn_str: str) -> None:
    """为 SQLite 文件路径创建父目录（内存数据库和 file: URI 不操作）。"""
    if conn_str != ":memory:" and not conn_str.startswith("file:"):
        pathlib.Path(conn_str).parent.mkdir(parents=True, exist_ok=True)
