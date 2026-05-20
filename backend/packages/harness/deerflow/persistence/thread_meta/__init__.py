"""线程元数据（ThreadMeta）持久化子包 —— ORM 模型、抽象存储和具体实现。

本子包负责管理线程的元数据，包括线程标题、状态、所有者和自定义元数据。
采用策略模式，提供两种存储实现:
  - ThreadMetaRepository: 基于 SQLAlchemy 的 SQL 实现（sqlite / postgres）
  - MemoryThreadMetaStore: 基于 LangGraph BaseStore 的内存实现（memory 模式）

导出:
  - InvalidMetadataFilterError: 无效元数据过滤器异常
  - MemoryThreadMetaStore:      内存存储实现
  - ThreadMetaRepository:       SQL 存储实现
  - ThreadMetaRow:              线程元数据表的 ORM 模型
  - ThreadMetaStore:            抽象存储接口
  - make_thread_store:          工厂函数，根据配置创建合适的存储实现
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deerflow.persistence.thread_meta.base import InvalidMetadataFilterError, ThreadMetaStore
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.thread_meta.sql import ThreadMetaRepository

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "InvalidMetadataFilterError",
    "MemoryThreadMetaStore",
    "ThreadMetaRepository",
    "ThreadMetaRow",
    "ThreadMetaStore",
    "make_thread_store",
]


def make_thread_store(
    session_factory: async_sessionmaker[AsyncSession] | None,
    store: BaseStore | None = None,
) -> ThreadMetaStore:
    """根据可用的后端创建合适的 ThreadMetaStore。

    工厂函数逻辑:
      1. 如果有 session_factory（SQL 会话工厂）→ 使用 ThreadMetaRepository（SQL 后端）
      2. 如果只有 store（LangGraph BaseStore）→ 使用 MemoryThreadMetaStore（内存后端）
      3. 两者都没有 → 抛出 ValueError

    这种设计使上层代码无需关心底层存储实现，
    只需传入可用的依赖即可自动选择合适的实现。
    """
    if session_factory is not None:
        return ThreadMetaRepository(session_factory)
    if store is None:
        raise ValueError("make_thread_store requires either a session_factory (SQL) or a store (memory)")
    return MemoryThreadMetaStore(store)
