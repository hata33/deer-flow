"""异步检查点工厂。

为需要正确资源清理的长时间运行异步服务器提供异步上下文管理器。

支持的后端：内存（memory）、SQLite、PostgreSQL。

用法（如 FastAPI 生命周期）::

    from deerflow.agents.checkpointer.async_provider import make_checkpointer

    async with make_checkpointer() as checkpointer:
        app.state.checkpointer = checkpointer  # 未配置时使用 InMemorySaver

同步用法见 :mod:`deerflow.agents.checkpointer.provider`。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from langgraph.types import Checkpointer

from deerflow.agents.checkpointer.provider import (
    POSTGRES_CONN_REQUIRED,
    POSTGRES_INSTALL,
    SQLITE_INSTALL,
)
from deerflow.config.app_config import get_app_config
from deerflow.runtime.store._sqlite_utils import ensure_sqlite_parent_dir, resolve_sqlite_conn_str

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 异步工厂
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _async_checkpointer(config) -> AsyncIterator[Checkpointer]:
    """构造并管理检查点生命周期的异步上下文管理器。"""
    if config.type == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    if config.type == "sqlite":
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError as exc:
            raise ImportError(SQLITE_INSTALL) from exc

        conn_str = resolve_sqlite_conn_str(config.connection_string or "store.db")
        ensure_sqlite_parent_dir(conn_str)
        async with AsyncSqliteSaver.from_conn_string(conn_str) as saver:
            await saver.setup()
            yield saver
        return

    if config.type == "postgres":
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError as exc:
            raise ImportError(POSTGRES_INSTALL) from exc

        if not config.connection_string:
            raise ValueError(POSTGRES_CONN_REQUIRED)

        async with AsyncPostgresSaver.from_conn_string(config.connection_string) as saver:
            await saver.setup()
            yield saver
        return

    raise ValueError(f"Unknown checkpointer type: {config.type!r}")


# ---------------------------------------------------------------------------
# 公共异步上下文管理器
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def make_checkpointer() -> AsyncIterator[Checkpointer]:
    """异步上下文管理器，在调用者生命周期内产出检查点。

    进入时打开资源，退出时关闭——无全局状态::

        async with make_checkpointer() as checkpointer:
            app.state.checkpointer = checkpointer

    当 config.yaml 中未配置检查点时，产出 InMemorySaver。
    """

    config = get_app_config()

    if config.checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    async with _async_checkpointer(config.checkpointer) as saver:
        yield saver
