"""
SQLAlchemy 支持的 RunEventStore 实现。

将事件持久化到 ``run_events`` 表。追踪内容在 ``max_trace_content``
字节处截断，以避免数据库膨胀。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.models.run_event import RunEventRow
from deerflow.runtime.events.store.base import RunEventStore
from deerflow.runtime.user_context import AUTO, _AutoSentinel, get_current_user, resolve_user_id

logger = logging.getLogger(__name__)


class DbRunEventStore(RunEventStore):
    """数据库支持的运行事件存储实现。

    将事件持久化到数据库表中，支持用户隔离和查询过滤。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, max_trace_content: int = 10240):
        """初始化数据库运行事件存储。

        Args:
            session_factory: SQLAlchemy 异步会话工厂
            max_trace_content: 追踪内容最大字节数
        """
        self._sf = session_factory
        self._max_trace_content = max_trace_content

    @staticmethod
    def _row_to_dict(row: RunEventRow) -> dict:
        """将数据库行转换为字典。

        Args:
            row: RunEventRow 数据库行对象

        Returns:
            事件字典
        """
        d = row.to_dict()
        d["metadata"] = d.pop("event_metadata", {})
        val = d.get("created_at")
        if isinstance(val, datetime):
            d["created_at"] = val.isoformat()
        d.pop("id", None)
        # 恢复写入时 JSON 序列化的结构化内容
        raw = d.get("content", "")
        metadata = d.get("metadata", {})
        if isinstance(raw, str) and (metadata.get("content_is_json") or metadata.get("content_is_dict")):
            try:
                d["content"] = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                # 内容看起来像 JSON 但解析失败；保持原始字符串
                logger.debug("Failed to deserialize content as JSON for event seq=%s", d.get("seq"))
        return d

    def _truncate_trace(self, category: str, content: Any, metadata: dict | None) -> tuple[Any, dict]:
        """截断追踪内容以避免数据库膨胀。

        Args:
            category: 事件类别
            content: 事件内容
            metadata: 元数据

        Returns:
            (截断后的内容, 更新后的元数据) 元组
        """
        if category == "trace":
            text = content if isinstance(content, str) else json.dumps(content, default=str, ensure_ascii=False)
            encoded = text.encode("utf-8")
            if len(encoded) > self._max_trace_content:
                # 按字节截断，然后解码（可能会切断多字节字符，所以使用 errors="ignore"）
                content = encoded[: self._max_trace_content].decode("utf-8", errors="ignore")
                metadata = {**(metadata or {}), "content_truncated": True, "original_byte_length": len(encoded)}
        return content, metadata or {}

    @staticmethod
    def _content_to_db(content: Any, metadata: dict | None) -> tuple[str, dict]:
        """将内容转换为数据库格式。

        Args:
            content: 事件内容
            metadata: 元数据

        Returns:
            (数据库内容, 更新后的元数据) 元组
        """
        metadata = metadata or {}
        if isinstance(content, str):
            return content, metadata

        db_content = json.dumps(content, default=str, ensure_ascii=False)
        metadata = {**metadata, "content_is_json": True}
        if isinstance(content, dict):
            metadata["content_is_dict"] = True
        return db_content, metadata

    @staticmethod
    def _user_id_from_context() -> str | None:
        """从上下文变量软读取 user_id 用于写入路径。

        Returns:
            用户 ID 字符串，如果上下文变量未设置则返回 None

        Note:
            如果上下文变量未设置，返回 ``None``（无过滤器/无标记），
            这是后台工作器写入的预期情况。HTTP 请求写入将具有由
            认证中间件设置的上下文变量，并自动标记其 user_id。

            在边界处将 ``user.id`` 强制转换为 ``str``：``User.id`` 在
            认证层类型为 ``UUID``，但 ``run_events.user_id`` 是 ``VARCHAR(64)``，
            aiosqlite 不能将原始 UUID 对象绑定到 VARCHAR 列
            （"type 'UUID' is not supported"）—— INSERT 会静默回滚，
            工作器会挂起。
        """
        user = get_current_user()
        return str(user.id) if user is not None else None

    @staticmethod
    async def _max_seq_for_thread(session: AsyncSession, thread_id: str) -> int | None:
        """Return the current max seq while serializing writers per thread.

        PostgreSQL rejects ``SELECT max(...) FOR UPDATE`` because aggregate
        results are not lockable rows. As a release-safe workaround, take a
        transaction-level advisory lock keyed by thread_id before reading the
        aggregate. Other dialects keep the existing row-locking statement.
        """
        stmt = select(func.max(RunEventRow.seq)).where(RunEventRow.thread_id == thread_id)
        bind = session.get_bind()
        dialect_name = bind.dialect.name if bind is not None else ""

        if dialect_name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(CAST(:thread_id AS text))::bigint)"),
                {"thread_id": thread_id},
            )
            return await session.scalar(stmt)

        return await session.scalar(stmt.with_for_update())

    async def put(self, *, thread_id, run_id, event_type, category, content="", metadata=None, created_at=None):  # noqa: D401
        """写入单个事件 —— 仅限低频路径。

        Note:
            这会打开一个带有 FOR UPDATE 锁的专用事务来分配单调的 *seq*。
            对于高吞吐量写入，请使用 :meth:`put_batch`，它为整个批次获取一次锁。
            目前唯一的调用者是 ``worker.run_agent``，用于初始的 ``human_message``
            事件（每个运行一次）。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            event_type: 事件类型
            category: 事件类别
            content: 事件内容
            metadata: 元数据
            created_at: 创建时间

        Returns:
            完整的事件记录字典
        """
        content, metadata = self._truncate_trace(category, content, metadata)
        db_content, metadata = self._content_to_db(content, metadata)
        user_id = self._user_id_from_context()
        async with self._sf() as session:
            async with session.begin():
                max_seq = await self._max_seq_for_thread(session, thread_id)
                seq = (max_seq or 0) + 1
                row = RunEventRow(
                    thread_id=thread_id,
                    run_id=run_id,
                    user_id=user_id,
                    event_type=event_type,
                    category=category,
                    content=db_content,
                    event_metadata=metadata,
                    seq=seq,
                    created_at=datetime.fromisoformat(created_at) if created_at else datetime.now(UTC),
                )
                session.add(row)
            return self._row_to_dict(row)

    async def put_batch(self, events):
        """批量写入事件。

        Args:
            events: 事件字典列表

        Returns:
            完整的事件记录字典列表

        Note:
            获取线程的最大 seq（假设批次中的所有事件属于同一线程）。
            注意：SQLite 上的聚合 with_for_update() 是空操作；
            UNIQUE(thread_id, seq) 约束在那里捕获竞争。
        """
        if not events:
            return []
        user_id = self._user_id_from_context()
        async with self._sf() as session:
            async with session.begin():
                # Get max seq for the thread (assume all events in batch belong to same thread).
                thread_id = events[0]["thread_id"]
                max_seq = await self._max_seq_for_thread(session, thread_id)
                seq = max_seq or 0
                rows = []
                for e in events:
                    seq += 1
                    content = e.get("content", "")
                    category = e.get("category", "trace")
                    metadata = e.get("metadata")
                    content, metadata = self._truncate_trace(category, content, metadata)
                    db_content, metadata = self._content_to_db(content, metadata)
                    row = RunEventRow(
                        thread_id=e["thread_id"],
                        run_id=e["run_id"],
                        user_id=e.get("user_id", user_id),
                        event_type=e["event_type"],
                        category=category,
                        content=db_content,
                        event_metadata=metadata,
                        seq=seq,
                        created_at=datetime.fromisoformat(e["created_at"]) if e.get("created_at") else datetime.now(UTC),
                    )
                    session.add(row)
                    rows.append(row)
            return [self._row_to_dict(r) for r in rows]

    async def list_messages(
        self,
        thread_id,
        *,
        limit=50,
        before_seq=None,
        after_seq=None,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        """返回线程的可显示消息。

        Args:
            thread_id: 线程 ID
            limit: 返回记录数量限制
            before_seq: 返回 seq < before_seq 的记录
            after_seq: 返回 seq > after_seq 的记录
            user_id: 用户 ID 过滤器

        Returns:
            消息字典列表
        """
        resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.list_messages")
        stmt = select(RunEventRow).where(RunEventRow.thread_id == thread_id, RunEventRow.category == "message")
        if resolved_user_id is not None:
            stmt = stmt.where(RunEventRow.user_id == resolved_user_id)
        if before_seq is not None:
            stmt = stmt.where(RunEventRow.seq < before_seq)
        if after_seq is not None:
            stmt = stmt.where(RunEventRow.seq > after_seq)

        if after_seq is not None:
            # 前向分页：光标后的前 `limit` 条记录
            stmt = stmt.order_by(RunEventRow.seq.asc()).limit(limit)
            async with self._sf() as session:
                result = await session.execute(stmt)
                return [self._row_to_dict(r) for r in result.scalars()]
        else:
            # before_seq 或默认（最新）：取最后 `limit` 条记录，升序返回
            stmt = stmt.order_by(RunEventRow.seq.desc()).limit(limit)
            async with self._sf() as session:
                result = await session.execute(stmt)
                rows = list(result.scalars())
                return [self._row_to_dict(r) for r in reversed(rows)]

    async def list_events(
        self,
        thread_id,
        run_id,
        *,
        event_types=None,
        limit=500,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        """返回运行的完整事件流。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            event_types: 可选的事件类型过滤器
            limit: 返回记录数量限制
            user_id: 用户 ID 过滤器

        Returns:
            事件字典列表
        """
        resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.list_events")
        stmt = select(RunEventRow).where(RunEventRow.thread_id == thread_id, RunEventRow.run_id == run_id)
        if resolved_user_id is not None:
            stmt = stmt.where(RunEventRow.user_id == resolved_user_id)
        if event_types:
            stmt = stmt.where(RunEventRow.event_type.in_(event_types))
        stmt = stmt.order_by(RunEventRow.seq.asc()).limit(limit)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def list_messages_by_run(
        self,
        thread_id,
        run_id,
        *,
        limit=50,
        before_seq=None,
        after_seq=None,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        """返回特定运行的可显示消息。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            limit: 返回记录数量限制
            before_seq: 返回 seq < before_seq 的记录
            after_seq: 返回 seq > after_seq 的记录
            user_id: 用户 ID 过滤器

        Returns:
            消息字典列表
        """
        resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.list_messages_by_run")
        stmt = select(RunEventRow).where(
            RunEventRow.thread_id == thread_id,
            RunEventRow.run_id == run_id,
            RunEventRow.category == "message",
        )
        if resolved_user_id is not None:
            stmt = stmt.where(RunEventRow.user_id == resolved_user_id)
        if before_seq is not None:
            stmt = stmt.where(RunEventRow.seq < before_seq)
        if after_seq is not None:
            stmt = stmt.where(RunEventRow.seq > after_seq)

        if after_seq is not None:
            stmt = stmt.order_by(RunEventRow.seq.asc()).limit(limit)
            async with self._sf() as session:
                result = await session.execute(stmt)
                return [self._row_to_dict(r) for r in result.scalars()]
        else:
            stmt = stmt.order_by(RunEventRow.seq.desc()).limit(limit)
            async with self._sf() as session:
                result = await session.execute(stmt)
                rows = list(result.scalars())
                return [self._row_to_dict(r) for r in reversed(rows)]

    async def count_messages(
        self,
        thread_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        """计算线程中的可显示消息数量。

        Args:
            thread_id: 线程 ID
            user_id: 用户 ID 过滤器

        Returns:
            消息数量
        """
        resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.count_messages")
        stmt = select(func.count()).select_from(RunEventRow).where(RunEventRow.thread_id == thread_id, RunEventRow.category == "message")
        if resolved_user_id is not None:
            stmt = stmt.where(RunEventRow.user_id == resolved_user_id)
        async with self._sf() as session:
            return await session.scalar(stmt) or 0

    async def delete_by_thread(
        self,
        thread_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        """删除线程的所有事件。

        Args:
            thread_id: 线程 ID
            user_id: 用户 ID 过滤器

        Returns:
            删除的事件数量
        """
        resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.delete_by_thread")
        async with self._sf() as session:
            count_conditions = [RunEventRow.thread_id == thread_id]
            if resolved_user_id is not None:
                count_conditions.append(RunEventRow.user_id == resolved_user_id)
            count_stmt = select(func.count()).select_from(RunEventRow).where(*count_conditions)
            count = await session.scalar(count_stmt) or 0
            if count > 0:
                await session.execute(delete(RunEventRow).where(*count_conditions))
                await session.commit()
            return count

    async def delete_by_run(
        self,
        thread_id,
        run_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        """删除特定运行的所有事件。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            user_id: 用户 ID 过滤器

        Returns:
            删除的事件数量
        """
        resolved_user_id = resolve_user_id(user_id, method_name="DbRunEventStore.delete_by_run")
        async with self._sf() as session:
            count_conditions = [RunEventRow.thread_id == thread_id, RunEventRow.run_id == run_id]
            if resolved_user_id is not None:
                count_conditions.append(RunEventRow.user_id == resolved_user_id)
            count_stmt = select(func.count()).select_from(RunEventRow).where(*count_conditions)
            count = await session.scalar(count_stmt) or 0
            if count > 0:
                await session.execute(delete(RunEventRow).where(*count_conditions))
                await session.commit()
            return count
