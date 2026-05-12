"""带有防抖机制的记忆更新队列。

收集会话上下文并在可配置的防抖周期后处理。
在防抖窗口内收到的多个会话会批量处理。
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from deerflow.config.memory_config import get_memory_config

logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """待处理的记忆更新会话上下文。"""

    thread_id: str  # 线程 ID
    messages: list[Any]  # 对话消息列表
    timestamp: datetime = field(default_factory=datetime.utcnow)  # 时间戳
    agent_name: str | None = None  # 智能体名称（按智能体存储记忆）


class MemoryUpdateQueue:
    """带有防抖机制的记忆更新队列。

    此队列收集会话上下文，并在可配置的防抖期后处理它们。
    在防抖窗口内收到的多个会话会批量处理。
    """

    def __init__(self):
        """Initialize the memory update queue."""
        self._queue: list[ConversationContext] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._processing = False

    def add(self, thread_id: str, messages: list[Any], agent_name: str | None = None) -> None:
        """添加会话到更新队列。

        Args:
            thread_id: 线程 ID。
            messages: 对话消息列表。
            agent_name: 如果提供，按智能体存储记忆；如果为 None，使用全局记忆。
        """
        config = get_memory_config()
        if not config.enabled:
            return

        context = ConversationContext(
            thread_id=thread_id,
            messages=messages,
            agent_name=agent_name,
        )

        with self._lock:
            # Check if this thread already has a pending update
            # If so, replace it with the newer one
            self._queue = [c for c in self._queue if c.thread_id != thread_id]
            self._queue.append(context)

            # Reset or start the debounce timer
            self._reset_timer()

        logger.info("Memory update queued for thread %s, queue size: %d", thread_id, len(self._queue))

    def _reset_timer(self) -> None:
        """重置防抖计时器。"""
        config = get_memory_config()

        # Cancel existing timer if any
        if self._timer is not None:
            self._timer.cancel()

        # Start new timer
        self._timer = threading.Timer(
            config.debounce_seconds,
            self._process_queue,
        )
        self._timer.daemon = True
        self._timer.start()

        logger.debug("Memory update timer set for %ss", config.debounce_seconds)

    def _process_queue(self) -> None:
        """处理队列中所有待处理的会话上下文。"""
        # Import here to avoid circular dependency
        from deerflow.agents.memory.updater import MemoryUpdater

        with self._lock:
            if self._processing:
                # Already processing, reschedule
                self._reset_timer()
                return

            if not self._queue:
                return

            self._processing = True
            contexts_to_process = self._queue.copy()
            self._queue.clear()
            self._timer = None

        logger.info("Processing %d queued memory updates", len(contexts_to_process))

        try:
            updater = MemoryUpdater()

            for context in contexts_to_process:
                try:
                    logger.info("Updating memory for thread %s", context.thread_id)
                    success = updater.update_memory(
                        messages=context.messages,
                        thread_id=context.thread_id,
                        agent_name=context.agent_name,
                    )
                    if success:
                        logger.info("Memory updated successfully for thread %s", context.thread_id)
                    else:
                        logger.warning("Memory update skipped/failed for thread %s", context.thread_id)
                except Exception as e:
                    logger.error("Error updating memory for thread %s: %s", context.thread_id, e)

                # Small delay between updates to avoid rate limiting
                if len(contexts_to_process) > 1:
                    time.sleep(0.5)

        finally:
            with self._lock:
                self._processing = False

    def flush(self) -> None:
        """强制立即处理队列。在测试或优雅关闭时使用。"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

        self._process_queue()

    def clear(self) -> None:
        """清空队列但不处理。在测试时使用。"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._queue.clear()
            self._processing = False

    @property
    def pending_count(self) -> int:
        """获取待处理更新的数量。"""
        with self._lock:
            return len(self._queue)

    @property
    def is_processing(self) -> bool:
        """检查队列是否正在处理中。"""
        with self._lock:
            return self._processing


# 全局单例实例
_memory_queue: MemoryUpdateQueue | None = None
_queue_lock = threading.Lock()


def get_memory_queue() -> MemoryUpdateQueue:
    """获取全局记忆更新队列单例。"""
    global _memory_queue
    with _queue_lock:
        if _memory_queue is None:
            _memory_queue = MemoryUpdateQueue()
        return _memory_queue


def reset_memory_queue() -> None:
    """重置全局记忆队列。在测试时使用。"""
    global _memory_queue
    with _queue_lock:
        if _memory_queue is not None:
            _memory_queue.clear()
        _memory_queue = None
