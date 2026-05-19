"""
同步检查点工厂模块。

为 LangGraph 图编译和 CLI 工具提供**同步单例**和**同步上下文管理器**。

支持的后端: memory, sqlite, postgres。

用法::

    from deerflow.runtime.checkpointer.provider import get_checkpointer, checkpointer_context

    # 单例 —— 跨调用重用，在进程退出时关闭
    cp = get_checkpointer()

    # 一次性 —— 新连接，在块退出时关闭
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
from deerflow.runtime.store._sqlite_utils import ensure_sqlite_parent_dir, resolve_sqlite_conn_str

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 错误消息常量 —— 也被 aio.provider 导入
# ---------------------------------------------------------------------------

# SQLite 安装提示
SQLITE_INSTALL = "langgraph-checkpoint-sqlite is required for the SQLite checkpointer. Install it with: uv add langgraph-checkpoint-sqlite"

# PostgreSQL 安装提示
POSTGRES_INSTALL = (
    "langgraph-checkpoint-postgres is required for the PostgreSQL checkpointer. Install the package extra with: pip install 'deerflow-harness[postgres]' (or use: uv sync --all-packages --extra postgres when developing locally)"
)

# PostgreSQL 连接字符串错误
POSTGRES_CONN_REQUIRED = "checkpointer.connection_string is required for the postgres backend"

# ---------------------------------------------------------------------------
# 同步工厂
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _sync_checkpointer_cm(config: CheckpointerConfig) -> Iterator[Checkpointer]:
    """创建和拆除同步检查点的上下文管理器。

    Args:
        config: 检查点配置

    Yields:
        Checkpointer 实例

    Note:
        返回配置的 ``Checkpointer`` 实例。任何底层连接或池的资源清理
        由此模块中的更高级别助手（如单例工厂或上下文管理器）处理；
        此函数不返回单独的清理回调。
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
        ensure_sqlite_parent_dir(conn_str)
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

# 全局检查点实例
_checkpointer: Checkpointer | None = None
# 保持连接活动的打开上下文管理器
_checkpointer_ctx = None


def get_checkpointer() -> Checkpointer:
    """返回全局同步检查点单例，在首次调用时创建。

    Returns:
        Checkpointer 实例（如果未配置则返回 InMemorySaver）

    Raises:
        ImportError: 如果未安装配置后端所需的包
        ValueError: 如果需要连接字符串的后端缺少它

    Note:
        当在 *config.yaml* 中未配置检查点时返回 ``InMemorySaver``。
    """
    global _checkpointer, _checkpointer_ctx

    if _checkpointer is not None:
        return _checkpointer

    # 在检查检查点配置之前确保加载应用配置
    # 这防止当 config.yaml 实际上有检查点节但尚未加载时返回 InMemorySaver
    from deerflow.config.app_config import _app_config
    from deerflow.config.checkpointer_config import get_checkpointer_config

    config = get_checkpointer_config()

    if config is None and _app_config is None:
        # 仅在应用配置和显式检查点配置都尚未初始化时延迟加载应用配置。
        # 这使有意设置全局检查点配置的测试与磁盘上的任何环境 config.yaml 隔离。
        try:
            get_app_config()
        except FileNotFoundError:
            # 在没有 config.yaml 的测试环境中，这是预期的。
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
    """重置同步单例，强制在下次调用时重新创建。

    Note:
        关闭任何打开的后端连接并清除缓存的实例。
        在测试中或配置更改后很有用。
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
    """同步上下文管理器，产生检查点并在退出时清理。

    与 :func:`get_checkpointer` 不同，这**不**缓存实例 ——
    每个 ``with`` 块创建并销毁自己的连接。在需要确定性清理的
    CLI 脚本或测试中使用它::

        with checkpointer_context() as cp:
            graph.invoke(input, config={"configurable": {"thread_id": "1"}})

    Yields:
        Checkpointer 实例（如果未配置则返回 InMemorySaver）

    Note:
        当在 *config.yaml* 中未配置检查点时产生 ``InMemorySaver``。
    """

    config = get_app_config()
    if config.checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    with _sync_checkpointer_cm(config.checkpointer) as saver:
        yield saver
