"""流桥抽象协议。

StreamBridge 解耦 Agent 工作线程（生产者）和 SSE 端点（消费者），
与 LangGraph Platform 的 Queue + StreamManager 架构对齐。
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StreamEvent:
    """单个流事件。

    Attributes:
        id: 单调递增的事件 ID（用作 SSE ``id:`` 字段，支持 Last-Event-ID 重连）。
        event: SSE 事件名称（如 metadata、updates、events、error、end）。
        data: JSON 可序列化载荷。
    """

    id: str
    event: str
    data: Any


HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)
END_SENTINEL = StreamEvent(id="", event="__end__", data=None)


class StreamBridge(abc.ABC):
    """流桥抽象基类。"""

    @abc.abstractmethod
    async def publish(self, run_id: str, event: str, data: Any) -> None:
        """为指定 run_id 入队一个事件（生产者侧）。"""

    @abc.abstractmethod
    async def publish_end(self, run_id: str) -> None:
        """通知不再为指定 run_id 产生更多事件。"""

    @abc.abstractmethod
    def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        """异步迭代器，yield 指定 run_id 的事件（消费者侧）。

        heartbeat_interval 秒内无事件时 yield HEARTBEAT_SENTINEL。
        生产者调用 publish_end 后 yield END_SENTINEL。
        """

    @abc.abstractmethod
    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        """释放指定 run_id 的资源。delay > 0 时延迟释放。"""

    async def close(self) -> None:
        """释放后端资源（默认无操作）。"""
