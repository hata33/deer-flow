"""
运行事件存储模块。

提供运行事件存储的抽象和实现，以及根据配置创建存储实例的工厂函数。
"""

# 从基础存储模块导入运行事件存储抽象
from deerflow.runtime.events.store.base import RunEventStore

# 从内存存储模块导入内存运行事件存储实现
from deerflow.runtime.events.store.memory import MemoryRunEventStore


def make_run_event_store(config=None) -> RunEventStore:
    """根据 run_events.backend 配置创建运行事件存储。

    Args:
        config: 运行事件配置对象

    Returns:
        RunEventStore 实例

    Raises:
        ValueError: 如果配置了未知的后端类型

    Note:
        支持的后端类型:
        - "memory": 内存存储（默认）
        - "db": 数据库存储
        - "jsonl": JSONL 文件存储
    """
    if config is None or config.backend == "memory":
        return MemoryRunEventStore()
    if config.backend == "db":
        from deerflow.persistence.engine import get_session_factory

        sf = get_session_factory()
        if sf is None:
            # database.backend=memory 但 run_events.backend=db -> 回退到内存
            return MemoryRunEventStore()
        from deerflow.runtime.events.store.db import DbRunEventStore

        return DbRunEventStore(sf, max_trace_content=config.max_trace_content)
    if config.backend == "jsonl":
        from deerflow.runtime.events.store.jsonl import JsonlRunEventStore

        return JsonlRunEventStore()
    raise ValueError(f"Unknown run_events backend: {config.backend!r}")


__all__ = [
    "MemoryRunEventStore",    # 内存运行事件存储实现
    "RunEventStore",           # 运行事件存储抽象基类
    "make_run_event_store",    # 创建运行事件存储的工厂函数
]
