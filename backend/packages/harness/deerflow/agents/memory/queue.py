"""
记忆更新防抖队列（第 4 层：中间件调度）

本模块实现记忆系统的防抖队列机制，是四层架构中第 4 层的核心组件。

为什么需要防抖：
  用户快速连续对话（如 "帮我改这个 bug → 不对，应该用方案B → 再加个测试"），
  每句话触发一次 LLM 记忆更新会浪费 token 并产生矛盾的中间态。
  防抖队列等待 30s 无新消息后才触发更新，将连续对话合并为一次 LLM 调用。

防抖队列工作流程：
  1. MemoryMiddleware.after_agent() → queue.add(thread_id, messages, ...)
     → 同 (thread_id, user_id, agent_name) 已有待处理项 → 替换为最新消息
     → 合并 correction_detected / reinforcement_detected 标志
     → 重置 threading.Timer（默认 30s）

  2. Timer 到期 → _process_queue()
     → 取出所有待处理项 → 逐个调用 MemoryUpdater.update_memory()
     → 多上下文间 sleep 0.5s 避免 LLM rate limit

两个触发入口：
  - add()：标准防抖，等 30s 后处理（由 MemoryMiddleware 调用）
  - add_nowait()：立即处理（由 SummarizationMiddleware 的 memory_flush_hook 调用，
    因为 SummarizationMiddleware 即将丢弃消息，必须在此之前提取记忆）

为什么用 threading.Timer 而非 asyncio：
  - 防抖队列是全局单例，可能被多个协程/线程并发访问
  - threading.Timer 在独立线程触发，不依赖调用方的事件循环
  - 需要显式捕获 user_id：threading.Timer 触发时 ContextVar 不跨线程传播，
    所以在 add() 时就存入 ConversationContext

依赖关系：
  - memory_config.py：debounce_seconds 配置
  - updater.py：MemoryUpdater.update_memory()（延迟导入，避免循环依赖）
  - 被 memory_middleware.py 调用（标准防抖入口）
  - 被 summarization_hook.py 调用（立即处理入口）
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from deerflow.config.memory_config import get_memory_config

logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """待处理的对话上下文（队列中的条目）。

    在 add() 时创建并存储所有必要信息，包括 user_id。
    这是必须的，因为 threading.Timer 在独立线程触发，
    ContextVar 不会跨线程传播，所以必须在入队时就捕获 user_id。

    Attributes:
        thread_id: 线程 ID
        messages: 对话消息列表
        timestamp: 入队时间
        agent_name: 智能体名称（None 表示全局记忆）
        user_id: 用户 ID（在 add() 时从 ContextVar 捕获）
        correction_detected: 是否检测到纠错信号
        reinforcement_detected: 是否检测到正面反馈信号
    """

    thread_id: str
    messages: list[Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    agent_name: str | None = None
    user_id: str | None = None
    correction_detected: bool = False
    reinforcement_detected: bool = False


class MemoryUpdateQueue:
    """带防抖机制的记忆更新队列。

    队列以 (thread_id, user_id, agent_name) 为键去重，
    同一键的新消息替换旧消息，只保留最新的完整对话。
    Timer 到期后批量处理所有待处理项。

    线程安全：
    - 所有对 _queue 的操作通过 _lock 保护
    - Timer 在 daemon 线程上运行，不会阻止进程退出
    """

    def __init__(self):
        """初始化记忆更新队列。"""
        self._queue: list[ConversationContext] = []
        self._lock = threading.Lock()  # 保护 _queue 的线程锁
        self._timer: threading.Timer | None = None  # 防抖计时器
        self._processing = False  # 标记是否正在处理（防止并发处理）

    @staticmethod
    def _queue_key(
        thread_id: str,
        user_id: str | None,
        agent_name: str | None,
    ) -> tuple[str, str | None, str | None]:
        """生成防抖去重键。

        同 (thread_id, user_id, agent_name) 的新消息替换旧消息，
        确保同一线程的连续对话只处理最新的一次。
        """
        return (thread_id, user_id, agent_name)

    def add(
        self,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None = None,
        user_id: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
    ) -> None:
        """添加对话到更新队列（标准防抖入口）。

        由 MemoryMiddleware.after_agent() 调用。
        同一键的新消息替换旧消息，并合并纠错/反馈标志。
        每次调用重置 Timer（防抖核心逻辑）。

        Args:
            thread_id: 线程 ID
            messages: 对话消息列表
            agent_name: 智能体名称（None 使用全局记忆）
            user_id: 用户 ID（在入队时捕获，跨线程安全）
            correction_detected: 是否检测到纠错信号
            reinforcement_detected: 是否检测到正面反馈信号
        """
        config = get_memory_config()
        if not config.enabled:
            return

        with self._lock:
            self._enqueue_locked(
                thread_id=thread_id,
                messages=messages,
                agent_name=agent_name,
                user_id=user_id,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
            )
            # 重置防抖计时器（新消息到来时重新计时）
            self._reset_timer()

        logger.info("Memory update queued for thread %s, queue size: %d", thread_id, len(self._queue))

    def add_nowait(
        self,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None = None,
        user_id: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
    ) -> None:
        """添加对话并立即开始处理（无防抖等待）。

        由 SummarizationMiddleware 的 memory_flush_hook 调用。
        因为 SummarizationMiddleware 即将丢弃消息，必须在此之前提取记忆，
        所以不能等 30s 防抖。

        内部通过设置 Timer 延迟为 0 实现"立即处理"。
        """
        config = get_memory_config()
        if not config.enabled:
            return

        with self._lock:
            self._enqueue_locked(
                thread_id=thread_id,
                messages=messages,
                agent_name=agent_name,
                user_id=user_id,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
            )
            # 延迟为 0，立即触发处理
            self._schedule_timer(0)

        logger.info("Memory update queued for immediate processing on thread %s, queue size: %d", thread_id, len(self._queue))

    def _enqueue_locked(
        self,
        *,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None,
        user_id: str | None,
        correction_detected: bool,
        reinforcement_detected: bool,
    ) -> None:
        """内部入队方法（调用方已持有 _lock）。

        逻辑：
        1. 查找同键的已有条目
        2. 合并纠错/反馈标志（任一次为 True 则保持 True）
        3. 移除旧条目，追加新条目（替换为最新消息）
        """
        queue_key = self._queue_key(thread_id, user_id, agent_name)
        existing_context = next(
            (context for context in self._queue if self._queue_key(context.thread_id, context.user_id, context.agent_name) == queue_key),
            None,
        )
        # 合并标志：只要有一次检测到信号，就保持 True
        merged_correction_detected = correction_detected or (existing_context.correction_detected if existing_context is not None else False)
        merged_reinforcement_detected = reinforcement_detected or (existing_context.reinforcement_detected if existing_context is not None else False)
        context = ConversationContext(
            thread_id=thread_id,
            messages=messages,
            agent_name=agent_name,
            user_id=user_id,
            correction_detected=merged_correction_detected,
            reinforcement_detected=merged_reinforcement_detected,
        )

        # 移除同键旧条目，追加新条目
        self._queue = [context for context in self._queue if self._queue_key(context.thread_id, context.user_id, context.agent_name) != queue_key]
        self._queue.append(context)

    def _reset_timer(self) -> None:
        """重置防抖计时器。

        每次有新消息入队时调用，重新开始等待 debounce_seconds。
        """
        config = get_memory_config()
        self._schedule_timer(config.debounce_seconds)

        logger.debug("Memory update timer set for %ss", config.debounce_seconds)

    def _schedule_timer(self, delay_seconds: float) -> None:
        """安排队列处理定时器。

        取消已有 Timer，创建新的 threading.Timer。
        daemon=True 确保不会阻止进程退出。
        """
        if self._timer is not None:
            self._timer.cancel()

        self._timer = threading.Timer(
            delay_seconds,
            self._process_queue,
        )
        self._timer.daemon = True
        self._timer.start()

    def _process_queue(self) -> None:
        """处理所有排队的对话上下文（Timer 到期时在独立线程中调用）。

        流程：
        1. 检查是否正在处理（防止并发）
        2. 取出所有待处理项并清空队列
        3. 逐个调用 MemoryUpdater.update_memory()
        4. 多个上下文间 sleep 0.5s 避免 LLM rate limit

        注意：MemoryUpdater 使用延迟导入（import inside function），
        避免与 updater.py 产生循环依赖。
        """
        # 延迟导入避免循环依赖（updater.py 导入 storage.py，而本模块被 middleware 调用）
        from deerflow.agents.memory.updater import MemoryUpdater

        with self._lock:
            if self._processing:
                # 正在处理中 → 重新安排立即处理（保留立即刷入语义）
                self._schedule_timer(0)
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
                        correction_detected=context.correction_detected,
                        reinforcement_detected=context.reinforcement_detected,
                        user_id=context.user_id,
                    )
                    if success:
                        logger.info("Memory updated successfully for thread %s", context.thread_id)
                    else:
                        logger.warning("Memory update skipped/failed for thread %s", context.thread_id)
                except Exception as e:
                    logger.error("Error updating memory for thread %s: %s", context.thread_id, e)

                # 多个上下文间短暂延迟，避免 LLM API 速率限制
                if len(contexts_to_process) > 1:
                    time.sleep(0.5)

        finally:
            with self._lock:
                self._processing = False

    def flush(self) -> None:
        """强制立即处理队列（同步等待完成）。

        用于测试或优雅关闭场景。
        取消当前 Timer 并同步调用 _process_queue()。
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

        self._process_queue()

    def flush_nowait(self) -> None:
        """在后台线程中立即开始处理队列（不等待完成）。

        通过设置 Timer 延迟为 0 实现。
        注意：daemon 线程，进程退出前队列中的消息可能丢失。
        对于 best-effort 的记忆更新，这是可接受的。
        """
        with self._lock:
            self._schedule_timer(0)

    def clear(self) -> None:
        """清空队列且不处理（取消 Timer）。

        用于测试场景。
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._queue.clear()
            self._processing = False

    @property
    def pending_count(self) -> int:
        """获取待处理的队列条目数。"""
        with self._lock:
            return len(self._queue)

    @property
    def is_processing(self) -> bool:
        """检查队列是否正在被处理。"""
        with self._lock:
            return self._processing


# ---- 全局单例 ----

_memory_queue: MemoryUpdateQueue | None = None
_queue_lock = threading.Lock()


def get_memory_queue() -> MemoryUpdateQueue:
    """获取全局记忆更新队列单例（线程安全的懒初始化）。

    使用双重检查锁定保证线程安全。
    """
    global _memory_queue
    with _queue_lock:
        if _memory_queue is None:
            _memory_queue = MemoryUpdateQueue()
        return _memory_queue


def reset_memory_queue() -> None:
    """重置全局记忆队列（清空并置空单例）。

    用于测试场景，确保测试间状态隔离。
    """
    global _memory_queue
    with _queue_lock:
        if _memory_queue is not None:
            _memory_queue.clear()
        _memory_queue = None
