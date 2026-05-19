"""异步存储工厂模块 —— 后端镜像配置的检查点。

存储和检查点共享 *config.yaml* 中的同一 ``checkpointer`` 节，
因此它们始终使用相同的持久化后端：

- ``type: memory``   → :class:`langgraph.store.memory.InMemoryStore`
- ``type: sqlite``   → :class:`langgraph.store.sqlite.aio.AsyncSqliteStore`
- ``type: postgres`` → :class:`langgraph.store.postgres.aio.AsyncPostgresStore`

用法（例如 FastAPI lifespan）::

    from deerflow.runtime.store import make_store

    async with make_store() as store:
        app.state.store = store
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from langgraph.store.base import BaseStore

from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.runtime.store.provider import POSTGRES_CONN_REQUIRED, POSTGRES_STORE_INSTALL, SQLITE_STORE_INSTALL, ensure_sqlite_parent_dir, resolve_sqlite_conn_str

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 内部后端工厂
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _async_store(config) -> AsyncIterator[BaseStore]:
    """构造和拆除存储的异步上下文管理器。

    Args:
        config: CheckpointerConfig 实例 —— 与检查点工厂使用的对象相同

    Yields:
        BaseStore 实例

    Raises:
        ImportError: 如果缺少所需的依赖
        ValueError: 如果配置无效
    """
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


# ---------------------------------------------------------------------------
# 公共异步上下文管理器
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def make_store(app_config: AppConfig | None = None) -> AsyncIterator[BaseStore]:
    """异步上下文管理器，产生后端与配置的检查点匹配的存储。

    从 *config.yaml* 的同一 ``checkpointer`` 节读取，由
    :func:`deerflow.runtime.checkpointer.async_provider.make_checkpointer` 使用，
    因此两个单例始终使用相同的持久化技术::

        async with make_store(app_config) as store:
            app.state.store = store

    Args:
        app_config: 应用配置，如果为 None 则使用全局配置

    Yields:
        BaseStore 实例

    Note:
        当未配置 ``checkpointer`` 节时产生
        :class:`~langgraph.store.memory.InMemoryStore`（在这种情况下发出警告）。
    """
    if app_config is None:
        app_config = get_app_config()

    if app_config.checkpointer is None:
        from langgraph.store.memory import InMemoryStore

        logger.warning("No 'checkpointer' section in config.yaml — using InMemoryStore for the store. Thread list will be lost on server restart. Configure a sqlite or postgres backend for persistence.")
        yield InMemoryStore()
        return

    async with _async_store(app_config.checkpointer) as store:
        yield store
