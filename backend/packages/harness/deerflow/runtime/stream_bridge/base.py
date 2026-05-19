"""抽象流桥接协议模块。

StreamBridge 将 agent 工作者（生产者）与 SSE 端点（消费者）解耦，
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
        id: 单调递增的事件 ID（用作 SSE ``id:`` 字段，支持 ``Last-Event-ID`` 重连）
        event: SSE 事件名称，例如 ``"metadata"``、``"updates"``、``"events"``、``"error"``、``"end"``
        data: JSON 可序列化的有效负载
    """

    id: str
    event: str
    data: Any


# 心跳哨兵事件
HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)

# 结束哨兵事件
END_SENTINEL = StreamEvent(id="", event="__end__", data=None)


class StreamBridge(abc.ABC):
    """流桥接的抽象基类。"""

    @abc.abstractmethod
    async def publish(self, run_id: str, event: str, data: Any) -> None:
        """为 *run_id* 将单个事件排队（生产者侧）。

        Args:
            run_id: 运行 ID
            event: 事件名称
            data: 事件数据
        """

    @abc.abstractmethod
    async def publish_end(self, run_id: str) -> None:
        """发出信号，表示不会为 *run_id* 产生更多事件。

        Args:
            run_id: 运行 ID
        """

    @abc.abstractmethod
    def subscribe(
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

    @abc.abstractmethod
    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        """释放与 *run_id* 关联的资源。

        Args:
            run_id: 运行 ID
            delay: 延迟秒数

        Note:
            如果 *delay* > 0，实现应该在释放前等待，给迟到的订阅者一个耗尽剩余事件的机会。
        """

    async def close(self) -> None:
        """释放后端资源。默认为空操作。"""
