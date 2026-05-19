"""由进程内事件日志支持的内存流桥接模块。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .base import END_SENTINEL, HEARTBEAT_SENTINEL, StreamBridge, StreamEvent

logger = logging.getLogger(__name__)


@dataclass
class _RunStream:
    """单个运行的流状态。

    Attributes:
        events: 事件列表
        condition: 异步条件变量
        ended: 是否已结束
        start_offset: 起始偏移量
    """
    events: list[StreamEvent] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    ended: bool = False
    start_offset: int = 0


class MemoryStreamBridge(StreamBridge):
    """每个运行的内存事件日志实现。

    事件在每个运行的有界时间窗口内保留，以便迟到的订阅者和
    重连的客户端可以从 ``Last-Event-ID`` 重放缓冲的事件。

    Attributes:
        _maxsize: 每个运行保留的最大事件数
        _streams: 运行 ID 到流状态的映射
        _counters: 运行 ID 到事件计数器的映射
    """

    def __init__(self, *, queue_maxsize: int = 256) -> None:
        """初始化内存流桥接。

        Args:
            queue_maxsize: 每个运行保留的最大事件数
        """
        self._maxsize = queue_maxsize
        self._streams: dict[str, _RunStream] = {}
        self._counters: dict[str, int] = {}

    # -- 辅助方法 ---------------------------------------------------------------

    def _get_or_create_stream(self, run_id: str) -> _RunStream:
        """获取或创建运行流。

        Args:
            run_id: 运行 ID

        Returns:
            运行流对象
        """
        if run_id not in self._streams:
            self._streams[run_id] = _RunStream()
            self._counters[run_id] = 0
        return self._streams[run_id]

    def _next_id(self, run_id: str) -> str:
        """生成下一个事件 ID。

        Args:
            run_id: 运行 ID

        Returns:
            事件 ID 字符串（格式：timestamp-sequence）
        """
        self._counters[run_id] = self._counters.get(run_id, 0) + 1
        ts = int(time.time() * 1000)
        seq = self._counters[run_id] - 1
        return f"{ts}-{seq}"

    def _resolve_start_offset(self, stream: _RunStream, last_event_id: str | None) -> int:
        """解析起始偏移量。

        Args:
            stream: 运行流对象
            last_event_id: 最后的事件 ID

        Returns:
            起始偏移量
        """
        if last_event_id is None:
            return stream.start_offset

        for index, entry in enumerate(stream.events):
            if entry.id == last_event_id:
                return stream.start_offset + index + 1

        if stream.events:
            logger.warning(
                "last_event_id=%s not found in retained buffer; replaying from earliest retained event",
                last_event_id,
            )
        return stream.start_offset

    # -- StreamBridge API ------------------------------------------------------

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        """为 *run_id* 将单个事件排队（生产者侧）。

        Args:
            run_id: 运行 ID
            event: 事件名称
            data: 事件数据
        """
        stream = self._get_or_create_stream(run_id)
        entry = StreamEvent(id=self._next_id(run_id), event=event, data=data)
        async with stream.condition:
            stream.events.append(entry)
            if len(stream.events) > self._maxsize:
                overflow = len(stream.events) - self._maxsize
                del stream.events[:overflow]
                stream.start_offset += overflow
            stream.condition.notify_all()

    async def publish_end(self, run_id: str) -> None:
        """发出信号，表示不会为 *run_id* 产生更多事件。

        Args:
            run_id: 运行 ID
        """
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            stream.ended = True
            stream.condition.notify_all()

    async def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        """异步迭代器，为 *run_id* 产生事件（消费者侧）。

        Args:
            run_id: 运行 ID
            last_event_id: 最后的事件 ID（用于重连）
            heartbeat_interval: 心跳间隔（秒）

        Yields:
            StreamEvent 对象

        Note:
            当在 *heartbeat_interval* 秒内没有事件到达时产生 :data:`HEARTBEAT_SENTINEL`。
            当生产者调用 :meth:`publish_end` 时产生 :data:`END_SENTINEL`。
        """
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            next_offset = self._resolve_start_offset(stream, last_event_id)

        while True:
            async with stream.condition:
                if next_offset < stream.start_offset:
                    logger.warning(
                        "subscriber for run %s fell behind retained buffer; resuming from offset %s",
                        run_id,
                        stream.start_offset,
                    )
                    next_offset = stream.start_offset

                local_index = next_offset - stream.start_offset
                if 0 <= local_index < len(stream.events):
                    entry = stream.events[local_index]
                    next_offset += 1
                elif stream.ended:
                    entry = END_SENTINEL
                else:
                    try:
                        await asyncio.wait_for(stream.condition.wait(), timeout=heartbeat_interval)
                    except TimeoutError:
                        entry = HEARTBEAT_SENTINEL
                    else:
                        continue

            if entry is END_SENTINEL:
                yield END_SENTINEL
                return
            yield entry

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        """释放与 *run_id* 关联的资源。

        Args:
            run_id: 运行 ID
            delay: 延迟秒数

        Note:
            如果 *delay* > 0，在释放前等待，给迟到的订阅者一个耗尽剩余事件的机会。
        """
        if delay > 0:
            await asyncio.sleep(delay)
        self._streams.pop(run_id, None)
        self._counters.pop(run_id, None)

    async def close(self) -> None:
        """释放后端资源。"""
        self._streams.clear()
        self._counters.clear()

