"""异步流桥接工厂模块。

提供与 :func:`deerflow.runtime.checkpointer.async_provider.make_checkpointer`
对齐的**异步上下文管理器**。

用法（例如 FastAPI lifespan）::

    from deerflow.agents.stream_bridge import make_stream_bridge

    async with make_stream_bridge() as bridge:
        app.state.stream_bridge = bridge
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from deerflow.config.app_config import AppConfig
from deerflow.config.stream_bridge_config import get_stream_bridge_config

from .base import StreamBridge

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def make_stream_bridge(app_config: AppConfig | None = None) -> AsyncIterator[StreamBridge]:
    """异步上下文管理器，产生 :class:`StreamBridge`。

    Args:
        app_config: 应用配置，如果为 None 则使用全局配置

    Yields:
        StreamBridge 实例

    Note:
        当未提供配置且未全局设置任何内容时，
        回退到 :class:`MemoryStreamBridge`。
    """
    if app_config is None:
        config = get_stream_bridge_config()
    else:
        config = app_config.stream_bridge

    if config is None or config.type == "memory":
        from deerflow.runtime.stream_bridge.memory import MemoryStreamBridge

        maxsize = config.queue_maxsize if config is not None else 256
        bridge = MemoryStreamBridge(queue_maxsize=maxsize)
        logger.info("Stream bridge initialised: memory (queue_maxsize=%d)", maxsize)
        try:
            yield bridge
        finally:
            await bridge.close()
        return

    if config.type == "redis":
        raise NotImplementedError("Redis stream bridge planned for Phase 2")

    raise ValueError(f"Unknown stream bridge type: {config.type!r}")
