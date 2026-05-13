"""基于 asyncio.Queue 的内存流桥实现。

每个 run_id 拥有独立的队列，支持心跳和结束信号。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from .base import END_SENTINEL, HEARTBEAT_SENTINEL, StreamBridge, StreamEvent

logger = logging.getLogger(__name__)

_PUBLISH_TIMEOUT = 30.0  # 队列满时的等待超时


class MemoryStreamBridge(StreamBridge):
    """每个 run_id 独立队列的内存实现。"""

    def __init__(self, *, queue_maxsize: int = 256) -> None:
        self._maxsize = queue_maxsize
        self._queues: dict[str, asyncio.Queue[StreamEvent]] = {}
        self._counters: dict[str, int] = {}

    def _get_or_create_queue(self, run_id: str) -> asyncio.Queue[StreamEvent]:
        """获取或创建运行级队列。"""
        if run_id not in self._queues:
            self._queues[run_id] = asyncio.Queue(maxsize=self._maxsize)
            self._counters[run_id] = 0
        return self._queues[run_id]

    def _next_id(self, run_id: str) -> str:
        """生成单调递增的事件 ID（时间戳-序号）。"""
        self._counters[run_id] = self._counters.get(run_id, 0) + 1
        ts = int(time.time() * 1000)
        seq = self._counters[run_id] - 1
        return f"{ts}-{seq}"

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        """发布事件到队列（超时时丢弃并记录警告）。"""
        queue = self._get_or_create_queue(run_id)
        entry = StreamEvent(id=self._next_id(run_id), event=event, data=data)
        try:
            await asyncio.wait_for(queue.put(entry), timeout=_PUBLISH_TIMEOUT)
        except TimeoutError:
            logger.warning("Stream bridge queue full for run %s — dropping event %s", run_id, event)

    async def publish_end(self, run_id: str) -> None:
        """发布结束信号。"""
        queue = self._get_or_create_queue(run_id)
        try:
            await asyncio.wait_for(queue.put(END_SENTINEL), timeout=_PUBLISH_TIMEOUT)
        except TimeoutError:
            logger.warning("Stream bridge queue full for run %s — dropping END sentinel", run_id)

    async def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        """订阅事件流：yield 事件、心跳或结束信号。"""
        if last_event_id is not None:
            logger.debug("last_event_id=%s accepted but ignored (memory bridge has no replay)", last_event_id)

        queue = self._get_or_create_queue(run_id)
        while True:
            try:
                entry = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
            except TimeoutError:
                yield HEARTBEAT_SENTINEL
                continue
            if entry is END_SENTINEL:
                yield END_SENTINEL
                return
            yield entry

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        """延迟清理运行资源。"""
        if delay > 0:
            await asyncio.sleep(delay)
        self._queues.pop(run_id, None)
        self._counters.pop(run_id, None)

    async def close(self) -> None:
        """清空所有队列。"""
        self._queues.clear()
        self._counters.clear()
