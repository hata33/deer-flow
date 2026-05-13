"""流桥模块 — 解耦 Agent 工作线程与 SSE 端点。

StreamBridge 位于后台 Agent 任务（生产者）和 HTTP SSE 端点（消费者）之间，
提供发布/订阅/清理的抽象接口，以及基于 asyncio.Queue 的内存实现。
"""

from .async_provider import make_stream_bridge
from .base import END_SENTINEL, HEARTBEAT_SENTINEL, StreamBridge, StreamEvent
from .memory import MemoryStreamBridge

__all__ = [
    "END_SENTINEL",
    "HEARTBEAT_SENTINEL",
    "MemoryStreamBridge",
    "StreamBridge",
    "StreamEvent",
    "make_stream_bridge",
]
