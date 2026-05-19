"""同步存储工厂模块。

为 CLI 工具和嵌入式 :class:`~deerflow.client.DeerFlowClient` 提供
**同步单例**和**同步上下文管理器**。

后端镜像配置的检查点，因此两者始终使用相同的持久化技术。
支持的后端: memory, sqlite, postgres。

用法::

    from deerflow.runtime.store.provider import get_store, store_context

    # 单例 —— 跨调用重用，在进程退出时关闭
    store = get_store()

    # 一次性 —— 新连接，在块退出时关闭
    with store_context() as store:
        store.put(("ns",), "key", {"value": 1})
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

from langgraph.store.base import BaseStore

from deerflow.config.app_config import get_app_config
from deerflow.runtime.store._sqlite_utils import ensure_sqlite_parent_dir, resolve_sqlite_conn_str

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 错误消息常量
# ---------------------------------------------------------------------------

# SQLite 存储安装提示
SQLITE_STORE_INSTALL = "langgraph-checkpoint-sqlite is required for the SQLite store. Install it with: uv add langgraph-checkpoint-sqlite"

# PostgreSQL 存储安装提示
POSTGRES_STORE_INSTALL = (
    "langgraph-checkpoint-postgres is required for the PostgreSQL store. Install the package extra with: pip install 'deerflow-harness[postgres]' (or use: uv sync --all-packages --extra postgres when developing locally)"
)

# PostgreSQL 连接字符串错误
POSTGRES_CONN_REQUIRED = "checkpointer.connection_string is required for the postgres backend"

# ---------------------------------------------------------------------------
# 同步工厂
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _sync_store_cm(config) -> Iterator[BaseStore]:
    """创建和拆除同步存储的上下文管理器。

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
            from langgraph.store.sqlite import SqliteStore
        except ImportError as exc:
            raise ImportError(SQLITE_STORE_INSTALL) from exc

        conn_str = resolve_sqlite_conn_str(config.connection_string or "store.db")
        ensure_sqlite_parent_dir(conn_str)

        with SqliteStore.from_conn_string(conn_str) as store:
            store.setup()
            logger.info("Store: using SqliteStore (%s)", conn_str)
            yield store
        return

    if config.type == "postgres":
        try:
            from langgraph.store.postgres import PostgresStore  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(POSTGRES_STORE_INSTALL) from exc

        if not config.connection_string:
            raise ValueError(POSTGRES_CONN_REQUIRED)

        with PostgresStore.from_conn_string(config.connection_string) as store:
            store.setup()
            logger.info("Store: using PostgresStore")
            yield store
        return

    raise ValueError(f"Unknown store backend type: {config.type!r}")


# ---------------------------------------------------------------------------
# 同步单例
# ---------------------------------------------------------------------------

_store: BaseStore | None = None
_store_ctx = None  # 打开的上下文管理器保持连接活动


def get_store() -> BaseStore:
    """返回全局同步存储单例，在首次调用时创建。

    Returns:
        BaseStore 实例（如果未配置检查点则返回 InMemoryStore）

    Raises:
        ImportError: 如果未安装配置后端所需的包
        ValueError: 如果需要连接字符串的后端缺少它

    Note:
        当在 *config.yaml* 中未配置检查点时返回
        :class:`~langgraph.store.memory.InMemoryStore`（在这种情况下发出警告）。
    """
    global _store, _store_ctx

    if _store is not None:
        return _store

    # 延迟加载应用配置，镜像检查点单例模式，以便显式设置全局检查点配置的测试保持隔离
    from deerflow.config.app_config import _app_config
    from deerflow.config.checkpointer_config import get_checkpointer_config

    config = get_checkpointer_config()

    if config is None and _app_config is None:
        try:
            get_app_config()
        except FileNotFoundError:
            pass
        config = get_checkpointer_config()

    if config is None:
        from langgraph.store.memory import InMemoryStore

        logger.warning("No 'checkpointer' section in config.yaml — using InMemoryStore for the store. Thread list will be lost on server restart. Configure a sqlite or postgres backend for persistence.")
        _store = InMemoryStore()
        return _store

    _store_ctx = _sync_store_cm(config)
    _store = _store_ctx.__enter__()
    return _store


def reset_store() -> None:
    """重置同步单例，强制在下次调用时重新创建。

    Note:
        关闭任何打开的后端连接并清除缓存的实例。
        在测试中或配置更改后很有用。
    """
    global _store, _store_ctx
    if _store_ctx is not None:
        try:
            _store_ctx.__exit__(None, None, None)
        except Exception:
            logger.warning("Error during store cleanup", exc_info=True)
        _store_ctx = None
    _store = None


# ---------------------------------------------------------------------------
# 同步上下文管理器
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def store_context() -> Iterator[BaseStore]:
    """同步上下文管理器，产生存储并在退出时清理。

    与 :func:`get_store` 不同，这**不**缓存实例 —— 每个
    ``with`` 块创建并销毁自己的连接。在需要确定性清理的 CLI 脚本或测试中使用它::

        with store_context() as store:
            store.put(("threads",), thread_id, {...})

    Yields:
        BaseStore 实例（如果未配置检查点则返回 InMemoryStore）

    Note:
        当在 *config.yaml* 中未配置检查点时产生
        :class:`~langgraph.store.memory.InMemoryStore`。
    """
    config = get_app_config()
    if config.checkpointer is None:
        from langgraph.store.memory import InMemoryStore

        logger.warning("No 'checkpointer' section in config.yaml — using InMemoryStore for the store. Thread list will be lost on server restart. Configure a sqlite or postgres backend for persistence.")
        yield InMemoryStore()
        return

    with _sync_store_cm(config.checkpointer) as store:
        yield store
