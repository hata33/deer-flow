"""同步检查点工厂。

为 LangGraph 图编译和 CLI 工具提供同步单例和同步上下文管理器。

支持的后端：内存（memory）、SQLite、PostgreSQL。

用法::

    from deerflow.agents.checkpointer.provider import get_checkpointer, checkpointer_context

    # 单例模式 —— 跨调用复用，进程退出时关闭
    cp = get_checkpointer()

    # 一次性模式 —— 新建连接，代码块退出时关闭
    with checkpointer_context() as cp:
        graph.invoke(input, config={"configurable": {"thread_id": "1"}})
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

from langgraph.types import Checkpointer

from deerflow.config.app_config import get_app_config
from deerflow.config.checkpointer_config import CheckpointerConfig
from deerflow.runtime.store._sqlite_utils import resolve_sqlite_conn_str

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 错误消息常量 —— 同时被 aio.provider 导入使用
# ---------------------------------------------------------------------------

SQLITE_INSTALL = "langgraph-checkpoint-sqlite is required for the SQLite checkpointer. Install it with: uv add langgraph-checkpoint-sqlite"
POSTGRES_INSTALL = "langgraph-checkpoint-postgres is required for the PostgreSQL checkpointer. Install it with: uv add langgraph-checkpoint-postgres psycopg[binary] psycopg-pool"
POSTGRES_CONN_REQUIRED = "checkpointer.connection_string is required for the postgres backend"

# ---------------------------------------------------------------------------
# 同步工厂
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _sync_checkpointer_cm(config: CheckpointerConfig) -> Iterator[Checkpointer]:
    """创建并管理同步检查点生命周期的上下文管理器。

    返回已配置的 Checkpointer 实例。底层连接或连接池的资源清理
    由本模块中的上层辅助函数（如单例工厂或上下文管理器）处理。
    """
    if config.type == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info("Checkpointer: using InMemorySaver (in-process, not persistent)")
        yield InMemorySaver()
        return

    if config.type == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as exc:
            raise ImportError(SQLITE_INSTALL) from exc

        conn_str = resolve_sqlite_conn_str(config.connection_string or "store.db")
        with SqliteSaver.from_conn_string(conn_str) as saver:
            saver.setup()
            logger.info("Checkpointer: using SqliteSaver (%s)", conn_str)
            yield saver
        return

    if config.type == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:
            raise ImportError(POSTGRES_INSTALL) from exc

        if not config.connection_string:
            raise ValueError(POSTGRES_CONN_REQUIRED)

        with PostgresSaver.from_conn_string(config.connection_string) as saver:
            saver.setup()
            logger.info("Checkpointer: using PostgresSaver")
            yield saver
        return

    raise ValueError(f"Unknown checkpointer type: {config.type!r}")


# ---------------------------------------------------------------------------
# 同步单例
# ---------------------------------------------------------------------------

_checkpointer: Checkpointer | None = None  # 全局检查点实例
_checkpointer_ctx = None  # 保持连接活跃的已打开上下文管理器


def get_checkpointer() -> Checkpointer:
    """返回全局同步检查点单例，首次调用时创建。

    当 config.yaml 中未配置检查点时，返回 InMemorySaver。

    Raises:
        ImportError: 如果配置的后端所需的包未安装。
        ValueError: 如果需要 connection_string 的后端缺少该配置。
    """
    global _checkpointer, _checkpointer_ctx

    if _checkpointer is not None:
        return _checkpointer

    # Ensure app config is loaded before checking checkpointer config
    # This prevents returning InMemorySaver when config.yaml actually has a checkpointer section
    # but hasn't been loaded yet
    from deerflow.config.app_config import _app_config
    from deerflow.config.checkpointer_config import get_checkpointer_config

    config = get_checkpointer_config()

    if config is None and _app_config is None:
        # Only load app config lazily when neither the app config nor an explicit
        # checkpointer config has been initialized yet. This keeps tests that
        # intentionally set the global checkpointer config isolated from any
        # ambient config.yaml on disk.
        try:
            get_app_config()
        except FileNotFoundError:
            # In test environments without config.yaml, this is expected.
            pass
        config = get_checkpointer_config()
    if config is None:
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info("Checkpointer: using InMemorySaver (in-process, not persistent)")
        _checkpointer = InMemorySaver()
        return _checkpointer

    _checkpointer_ctx = _sync_checkpointer_cm(config)
    _checkpointer = _checkpointer_ctx.__enter__()

    return _checkpointer


def reset_checkpointer() -> None:
    """重置同步单例，强制下次调用时重新创建。

    关闭所有打开的后端连接并清除缓存的实例。
    在测试或配置变更后使用。
    """
    global _checkpointer, _checkpointer_ctx
    if _checkpointer_ctx is not None:
        try:
            _checkpointer_ctx.__exit__(None, None, None)
        except Exception:
            logger.warning("Error during checkpointer cleanup", exc_info=True)
        _checkpointer_ctx = None
    _checkpointer = None


# ---------------------------------------------------------------------------
# 同步上下文管理器
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def checkpointer_context() -> Iterator[Checkpointer]:
    """同步上下文管理器，产出检查点并在退出时清理资源。

    与 get_checkpointer 不同，此方法**不缓存**实例——
    每个 with 块都会创建和销毁自己的连接。
    在需要确定性清理的 CLI 脚本或测试中使用::

        with checkpointer_context() as cp:
            graph.invoke(input, config={"configurable": {"thread_id": "1"}})

    当 config.yaml 中未配置检查点时，产出 InMemorySaver。
    """

    config = get_app_config()
    if config.checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    with _sync_checkpointer_cm(config.checkpointer) as saver:
        yield saver
