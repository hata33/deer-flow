"""
内存运行事件存储。当 run_events.backend=memory（默认）和测试中使用。

对于单进程异步使用是线程安全的（不需要线程锁，
因为所有突变都在同一事件循环内发生）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from deerflow.runtime.events.store.base import RunEventStore


class MemoryRunEventStore(RunEventStore):
    """内存运行事件存储实现。

    将事件存储在内存字典中，适用于开发和测试环境。
    """

    def __init__(self) -> None:
        """初始化内存运行事件存储。"""
        self._events: dict[str, list[dict]] = {}  # thread_id -> 排序的事件列表
        self._seq_counters: dict[str, int] = {}  # thread_id -> 最后分配的 seq

    def _next_seq(self, thread_id: str) -> int:
        """获取线程的下一个序列号。

        Args:
            thread_id: 线程 ID

        Returns:
            下一个序列号
        """
        current = self._seq_counters.get(thread_id, 0)
        next_val = current + 1
        self._seq_counters[thread_id] = next_val
        return next_val

    def _put_one(
        self,
        *,
        thread_id: str,
        run_id: str,
        event_type: str,
        category: str,
        content: str | dict = "",
        metadata: dict | None = None,
        created_at: str | None = None,
    ) -> dict:
        """写入单个事件。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            event_type: 事件类型
            category: 事件类别
            content: 事件内容
            metadata: 元数据
            created_at: 创建时间

        Returns:
            完整的事件记录
        """
        seq = self._next_seq(thread_id)
        record = {
            "thread_id": thread_id,
            "run_id": run_id,
            "event_type": event_type,
            "category": category,
            "content": content,
            "metadata": metadata or {},
            "seq": seq,
            "created_at": created_at or datetime.now(UTC).isoformat(),
        }
        self._events.setdefault(thread_id, []).append(record)
        return record

    async def put(
        self,
        *,
        thread_id,
        run_id,
        event_type,
        category,
        content="",
        metadata=None,
        created_at=None,
    ):
        """写入单个事件。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            event_type: 事件类型
            category: 事件类别
            content: 事件内容
            metadata: 元数据
            created_at: 创建时间

        Returns:
            完整的事件记录
        """
        return self._put_one(
            thread_id=thread_id,
            run_id=run_id,
            event_type=event_type,
            category=category,
            content=content,
            metadata=metadata,
            created_at=created_at,
        )

    async def put_batch(self, events):
        """批量写入事件。

        Args:
            events: 事件字典列表

        Returns:
            完整的事件记录列表
        """
        results = []
        for ev in events:
            record = self._put_one(**ev)
            results.append(record)
        return results

    async def list_messages(self, thread_id, *, limit=50, before_seq=None, after_seq=None):
        """返回线程的可显示消息。

        Args:
            thread_id: 线程 ID
            limit: 返回记录数量限制
            before_seq: 返回 seq < before_seq 的记录
            after_seq: 返回 seq > after_seq 的记录

        Returns:
            消息字典列表
        """
        all_events = self._events.get(thread_id, [])
        messages = [e for e in all_events if e["category"] == "message"]

        if before_seq is not None:
            messages = [e for e in messages if e["seq"] < before_seq]
            # 取最后 `limit` 条记录
            return messages[-limit:]
        elif after_seq is not None:
            messages = [e for e in messages if e["seq"] > after_seq]
            return messages[:limit]
        else:
            # 返回最新的 `limit` 条记录，升序
            return messages[-limit:]

    async def list_events(self, thread_id, run_id, *, event_types=None, limit=500):
        """返回运行的完整事件流。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            event_types: 可选的事件类型过滤器
            limit: 返回记录数量限制

        Returns:
            事件字典列表
        """
        all_events = self._events.get(thread_id, [])
        filtered = [e for e in all_events if e["run_id"] == run_id]
        if event_types is not None:
            filtered = [e for e in filtered if e["event_type"] in event_types]
        return filtered[:limit]

    async def list_messages_by_run(self, thread_id, run_id, *, limit=50, before_seq=None, after_seq=None):
        """返回特定运行的可显示消息。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            limit: 返回记录数量限制
            before_seq: 返回 seq < before_seq 的记录
            after_seq: 返回 seq > after_seq 的记录

        Returns:
            消息字典列表
        """
        all_events = self._events.get(thread_id, [])
        filtered = [e for e in all_events if e["run_id"] == run_id and e["category"] == "message"]
        if before_seq is not None:
            filtered = [e for e in filtered if e["seq"] < before_seq]
        if after_seq is not None:
            filtered = [e for e in filtered if e["seq"] > after_seq]
        if after_seq is not None:
            return filtered[:limit]
        else:
            return filtered[-limit:] if len(filtered) > limit else filtered

    async def count_messages(self, thread_id):
        """计算线程中的可显示消息数量。

        Args:
            thread_id: 线程 ID

        Returns:
            消息数量
        """
        all_events = self._events.get(thread_id, [])
        return sum(1 for e in all_events if e["category"] == "message")

    async def delete_by_thread(self, thread_id):
        """删除线程的所有事件。

        Args:
            thread_id: 线程 ID

        Returns:
            删除的事件数量
        """
        events = self._events.pop(thread_id, [])
        self._seq_counters.pop(thread_id, None)
        return len(events)

    async def delete_by_run(self, thread_id, run_id):
        """删除特定运行的所有事件。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID

        Returns:
            删除的事件数量
        """
        all_events = self._events.get(thread_id, [])
        if not all_events:
            return 0
        remaining = [e for e in all_events if e["run_id"] != run_id]
        removed = len(all_events) - len(remaining)
        self._events[thread_id] = remaining
        return removed
