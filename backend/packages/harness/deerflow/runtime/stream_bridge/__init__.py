"""流桥接模块 —— 将 agent 工作者与 SSE 端点解耦。

``StreamBridge`` 位于运行 agent 的后台任务（生产者）和向客户端
推送服务器发送事件的 HTTP 端点（消费者）之间。此包提供抽象协议
（:class:`StreamBridge`）加上由 :mod:`asyncio.Queue` 支持的默认内存实现。
"""

# 从异步提供者模块导入流桥接创建函数
from .async_provider import make_stream_bridge

# 从基础模块导入流桥接抽象和相关常量
# - END_SENTINEL: 结束标记
# - HEARTBEAT_SENTINEL: 心跳标记
# - StreamBridge: 流桥接抽象基类
# - StreamEvent: 流事件类型
from .base import END_SENTINEL, HEARTBEAT_SENTINEL, StreamBridge, StreamEvent

# 从内存模块导入内存流桥接实现
from .memory import MemoryStreamBridge

__all__ = [
    "END_SENTINEL",          # 结束标记
    "HEARTBEAT_SENTINEL",    # 心跳标记
    "MemoryStreamBridge",    # 内存流桥接实现
    "StreamBridge",           # 流桥接抽象基类
    "StreamEvent",            # 流事件类型
    "make_stream_bridge",     # 创建流桥接实例的工厂函数
]
