"""LangGraph 兼容运行时 — 运行管理、流式传输和生命周期管理。

统一导出 runs、store、stream_bridge、serialization 模块的公开 API，
消费者可直接从 deerflow.runtime 导入。
"""

from .runs import ConflictError, DisconnectMode, RunManager, RunRecord, RunStatus, UnsupportedStrategyError, run_agent
from .serialization import serialize, serialize_channel_values, serialize_lc_object, serialize_messages_tuple
from .store import get_store, make_store, reset_store, store_context
from .stream_bridge import END_SENTINEL, HEARTBEAT_SENTINEL, MemoryStreamBridge, StreamBridge, StreamEvent, make_stream_bridge

__all__ = [
    # runs
    "ConflictError",
    "DisconnectMode",
    "RunManager",
    "RunRecord",
    "RunStatus",
    "UnsupportedStrategyError",
    "run_agent",
    # serialization
    "serialize",
    "serialize_channel_values",
    "serialize_lc_object",
    "serialize_messages_tuple",
    # store
    "get_store",
    "make_store",
    "reset_store",
    "store_context",
    # stream_bridge
    "END_SENTINEL",
    "HEARTBEAT_SENTINEL",
    "MemoryStreamBridge",
    "StreamBridge",
    "StreamEvent",
    "make_stream_bridge",
]
