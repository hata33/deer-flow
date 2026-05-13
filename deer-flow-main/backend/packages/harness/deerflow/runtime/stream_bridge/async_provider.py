"""异步流桥工厂。

根据配置创建 StreamBridge 实例，默认使用 MemoryStreamBridge。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from deerflow.config.stream_bridge_config import get_stream_bridge_config

from .base import StreamBridge

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def make_stream_bridge(config=None) -> AsyncIterator[StreamBridge]:
    """异步上下文管理器，yield 配置对应的 StreamBridge。

    无配置或 type=memory 时使用 MemoryStreamBridge，Redis 支持计划在 Phase 2。
    """
    if config is None:
        config = get_stream_bridge_config()

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
