"""
LangGraph 兼容的运行时模块 —— 提供运行管理、流式传输和生命周期管理功能。

该模块重新导出 :mod:`~deerflow.runtime.runs` 和 :mod:`~deerflow.runtime.stream_bridge`
的公共 API，使消费者可以直接从 ``deerflow.runtime`` 导入所需功能。

主要功能:
- runs: 运行管理和执行上下文
- checkpointer: 检查点管理，用于状态持久化
- store: 存储抽象层
- stream_bridge: 流式传输桥接
- serialization: 序列化工具
"""

# 检查点相关导入
# - checkpointer_context: 检查点上下文管理器
# - get_checkpointer: 获取当前检查点实例
# - make_checkpointer: 创建检查点实例
# - reset_checkpointer: 重置检查点
from .checkpointer import checkpointer_context, get_checkpointer, make_checkpointer, reset_checkpointer

# 运行管理相关导入
# - ConflictError: 冲突错误异常
# - DisconnectMode: 断开连接模式枚举
# - RunContext: 运行上下文
# - RunManager: 运行管理器
# - RunRecord: 运行记录
# - RunStatus: 运行状态枚举
# - UnsupportedStrategyError: 不支持的策略错误异常
# - run_agent: 运行 agent 的主函数
from .runs import ConflictError, DisconnectMode, RunContext, RunManager, RunRecord, RunStatus, UnsupportedStrategyError, run_agent

# 序列化相关导入
# - serialize: 通用序列化函数
# - serialize_channel_values: 序列化通道值
# - serialize_lc_object: 序列化 LangChain 对象
# - serialize_messages_tuple: 序列化消息元组
from .serialization import serialize, serialize_channel_values, serialize_lc_object, serialize_messages_tuple

# 存储相关导入
# - get_store: 获取当前存储实例
# - make_store: 创建存储实例
# - reset_store: 重置存储
# - store_context: 存储上下文管理器
from .store import get_store, make_store, reset_store, store_context

# 流桥接相关导入
# - END_SENTINEL: 结束标记
# - HEARTBEAT_SENTINEL: 心跳标记
# - MemoryStreamBridge: 内存流桥接实现
# - StreamBridge: 流桥接抽象基类
# - StreamEvent: 流事件类型
# - make_stream_bridge: 创建流桥接实例
from .stream_bridge import END_SENTINEL, HEARTBEAT_SENTINEL, MemoryStreamBridge, StreamBridge, StreamEvent, make_stream_bridge

__all__ = [
    # checkpointer
    "checkpointer_context",
    "get_checkpointer",
    "make_checkpointer",
    "reset_checkpointer",
    # runs
    "ConflictError",
    "DisconnectMode",
    "RunContext",
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
