"""
运行事件存储的抽象接口。

RunEventStore 是运行事件流的统一存储接口。
消息（前端显示）和执行追踪（调试/审计）通过同一接口，
由 ``category`` 字段区分。

实现:
- MemoryRunEventStore: 内存字典（开发、测试）
- 未来: DB 支持的存储（SQLAlchemy ORM）、JSONL 文件存储
"""

from __future__ import annotations

import abc


class RunEventStore(abc.ABC):
    """运行事件流存储接口。

    所有实现必须保证:
    1. put() 事件在后续查询中可检索
    2. seq 在同一线程内严格递增
    3. list_messages() 仅返回 category="message" 事件
    4. list_events() 返回指定运行的所有事件
    5. 返回的字典匹配 RunEvent 字段结构
    """

    @abc.abstractmethod
    async def put(
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
        """写入事件，自动分配 seq，返回完整记录。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            event_type: 事件类型
            category: 事件类别
            content: 事件内容
            metadata: 元数据
            created_at: 创建时间

        Returns:
            包含分配的 seq 的完整事件记录
        """

    @abc.abstractmethod
    async def put_batch(self, events: list[dict]) -> list[dict]:
        """批量写入事件。由 RunJournal 刷新缓冲区使用。

        Args:
            events: 事件字典列表，每个字典的键与 put() 的关键字参数匹配

        Returns:
            分配了 seq 的完整记录列表
        """

    @abc.abstractmethod
    async def list_messages(
        self,
        thread_id: str,
        *,
        limit: int = 50,
        before_seq: int | None = None,
        after_seq: int | None = None,
    ) -> list[dict]:
        """返回线程的可显示消息（category=message），按 seq 升序排列。

        Args:
            thread_id: 线程 ID
            limit: 返回记录数量限制
            before_seq: 返回 seq < before_seq 的最后 ``limit`` 条记录（升序）
            after_seq: 返回 seq > after_seq 的前 ``limit`` 条记录（升序）

        Returns:
            消息字典列表

        Note:
            支持双向光标分页:
            - before_seq: 返回 seq < before_seq 的最后 ``limit`` 条记录（升序）
            - after_seq: 返回 seq > after_seq 的前 ``limit`` 条记录（升序）
            - 都不提供: 返回最新的 ``limit`` 条记录（升序）
        """

    @abc.abstractmethod
    async def list_events(
        self,
        thread_id: str,
        run_id: str,
        *,
        event_types: list[str] | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """返回运行的完整事件流，按 seq 升序排列。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            event_types: 可选的事件类型过滤器
            limit: 返回记录数量限制

        Returns:
            事件字典列表

        Note:
            可选择按 event_types 过滤。
        """

    @abc.abstractmethod
    async def list_messages_by_run(
        self,
        thread_id: str,
        run_id: str,
        *,
        limit: int = 50,
        before_seq: int | None = None,
        after_seq: int | None = None,
    ) -> list[dict]:
        """返回特定运行的可显示消息（category=message），按 seq 升序排列。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            limit: 返回记录数量限制
            before_seq: 返回 seq < before_seq 的最后 ``limit`` 条记录（升序）
            after_seq: 返回 seq > after_seq 的前 ``limit`` 条记录（升序）

        Returns:
            消息字典列表

        Note:
            支持双向光标分页:
            - after_seq: 返回 seq > after_seq 的前 ``limit`` 条记录（升序）
            - before_seq: 返回 seq < before_seq 的最后 ``limit`` 条记录（升序）
            - 都不提供: 返回最新的 ``limit`` 条记录（升序）
        """

    @abc.abstractmethod
    async def count_messages(self, thread_id: str) -> int:
        """计算线程中的可显示消息数量（category=message）。

        Args:
            thread_id: 线程 ID

        Returns:
            消息数量
        """

    @abc.abstractmethod
    async def delete_by_thread(self, thread_id: str) -> int:
        """删除线程的所有事件。

        Args:
            thread_id: 线程 ID

        Returns:
            删除的事件数量
        """

    @abc.abstractmethod
    async def delete_by_run(self, thread_id: str, run_id: str) -> int:
        """删除特定运行的所有事件。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID

        Returns:
            删除的事件数量
        """
