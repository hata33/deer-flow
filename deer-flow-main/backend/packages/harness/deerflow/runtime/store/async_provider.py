"""异步 Store 工厂。

后端与配置的 checkpointer 共享同一配置段，确保持久化技术一致。
支持的类型：memory、sqlite、postgres。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from langgraph.store.base import BaseStore

from deerflow.config.app_config import get_app_config
from deerflow.runtime.store.provider import POSTGRES_CONN_REQUIRED, POSTGRES_STORE_INSTALL, SQLITE_STORE_INSTALL, ensure_sqlite_parent_dir, resolve_sqlite_conn_str

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _async_store(config) -> AsyncIterator[BaseStore]:
    """异步上下文管理器：构建并销毁 Store。"""
    if config.type == "memory":
        from langgraph.store.memory import InMemoryStore

        logger.info("Store: using InMemoryStore (in-process, not persistent)")
        yield InMemoryStore()
        return

    if config.type == "sqlite":
        try:
            from langgraph.store.sqlite.aio import AsyncSqliteStore
        except ImportError as exc:
            raise ImportError(SQLITE_STORE_INSTALL) from exc

        conn_str = resolve_sqlite_conn_str(config.connection_string or "store.db")
        ensure_sqlite_parent_dir(conn_str)

        async with AsyncSqliteStore.from_conn_string(conn_str) as store:
            await store.setup()
            logger.info("Store: using AsyncSqliteStore (%s)", conn_str)
            yield store
        return

    if config.type == "postgres":
        try:
            from langgraph.store.postgres.aio import AsyncPostgresStore  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(POSTGRES_STORE_INSTALL) from exc

        if not config.connection_string:
            raise ValueError(POSTGRES_CONN_REQUIRED)

        async with AsyncPostgresStore.from_conn_string(config.connection_string) as store:
            await store.setup()
            logger.info("Store: using AsyncPostgresStore")
            yield store
        return

    raise ValueError(f"Unknown store backend type: {config.type!r}")


@contextlib.asynccontextmanager
async def make_store() -> AsyncIterator[BaseStore]:
    """异步上下文管理器，yield 与 checkpointer 同后端的 Store。

    无 checkpointer 配置时 yield InMemoryStore 并发出警告。
    """
    config = get_app_config()

    if config.checkpointer is None:
        from langgraph.store.memory import InMemoryStore

        logger.warning("No 'checkpointer' section in config.yaml — using InMemoryStore for the store. Thread list will be lost on server restart. Configure a sqlite or postgres backend for persistence.")
        yield InMemoryStore()
        return

    async with _async_store(config.checkpointer) as store:
        yield store
