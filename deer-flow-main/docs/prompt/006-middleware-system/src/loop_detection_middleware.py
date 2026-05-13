"""用于检测和中断重复工具调用循环的中间件。

P0 安全保障：防止智能体无限期使用相同参数调用相同工具，直到递归限制终止运行。

检测策略：
  1. 每次模型响应后，对工具调用（name + args）进行哈希。
  2. 在滑动窗口中跟踪最近的哈希值。
  3. 如果同一哈希出现 >= warn_threshold 次，注入"你在重复自己——收尾"的系统消息（每个哈希仅一次）。
  4. 如果出现 >= hard_limit 次，从响应中剥离所有 tool_calls，强制智能体生成最终文本答案。
"""

import hashlib
import json
import logging
import threading
from collections import OrderedDict, defaultdict
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

# 默认值——可通过构造函数覆盖
_DEFAULT_WARN_THRESHOLD = 3  # 3 次相同调用后注入警告
_DEFAULT_HARD_LIMIT = 5  # 5 次相同调用后强制停止
_DEFAULT_WINDOW_SIZE = 20  # 跟踪最近 N 次工具调用
_DEFAULT_MAX_TRACKED_THREADS = 100  # LRU 驱逐限制


def _hash_tool_calls(tool_calls: list[dict]) -> str:
    """对一组工具调用（name + args）生成确定性哈希。

    此哈希与顺序无关：相同的多集工具调用应始终产生相同的哈希值，
    无论输入顺序如何。
    """
    # 首先将每个工具调用标准化为最小的 (name, args) 结构。
    normalized: list[dict] = []
    for tc in tool_calls:
        normalized.append(
            {
                "name": tc.get("name", ""),
                "args": tc.get("args", {}),
            }
        )

    # 按 name 和 args 的确定性序列化排序，使得相同多集的排列产生相同的顺序。
    normalized.sort(
        key=lambda tc: (
            tc["name"],
            json.dumps(tc["args"], sort_keys=True, default=str),
        )
    )
    blob = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.md5(blob.encode()).hexdigest()[:12]


_WARNING_MSG = "[LOOP DETECTED] You are repeating the same tool calls. Stop calling tools and produce your final answer now. If you cannot complete the task, summarize what you accomplished so far."

_HARD_STOP_MSG = "[FORCED STOP] Repeated tool calls exceeded the safety limit. Producing final answer with results collected so far."


class LoopDetectionMiddleware(AgentMiddleware[AgentState]):
    """检测并中断重复的工具调用循环。

    参数：
        warn_threshold: 注入警告消息前允许的相同工具调用集次数。默认值：3。
        hard_limit: 完全剥离 tool_calls 前允许的相同工具调用集次数。默认值：5。
        window_size: 跟踪调用的滑动窗口大小。默认值：20。
        max_tracked_threads: 在驱逐最近最少使用的线程前最多跟踪的线程数。默认值：100。
    """

    def __init__(
        self,
        warn_threshold: int = _DEFAULT_WARN_THRESHOLD,
        hard_limit: int = _DEFAULT_HARD_LIMIT,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        max_tracked_threads: int = _DEFAULT_MAX_TRACKED_THREADS,
    ):
        super().__init__()
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size
        self.max_tracked_threads = max_tracked_threads
        self._lock = threading.Lock()
        # 使用 OrderedDict 进行按线程跟踪，支持 LRU 驱逐
        self._history: OrderedDict[str, list[str]] = OrderedDict()
        self._warned: dict[str, set[str]] = defaultdict(set)

    def _get_thread_id(self, runtime: Runtime) -> str:
        """从运行时上下文中提取 thread_id，用于按线程跟踪。"""
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if thread_id:
            return thread_id
        return "default"

    def _evict_if_needed(self) -> None:
        """在超过限制时驱逐最近最少使用的线程。

        必须在持有 self._lock 的情况下调用。
        """
        while len(self._history) > self.max_tracked_threads:
            evicted_id, _ = self._history.popitem(last=False)
            self._warned.pop(evicted_id, None)
            logger.debug("Evicted loop tracking for thread %s (LRU)", evicted_id)

    def _track_and_check(self, state: AgentState, runtime: Runtime) -> tuple[str | None, bool]:
        """跟踪工具调用并检测循环。

        返回：
            (警告消息或 None, 是否强制停止)
        """
        messages = state.get("messages", [])
        if not messages:
            return None, False

        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None, False

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None, False

        thread_id = self._get_thread_id(runtime)
        call_hash = _hash_tool_calls(tool_calls)

        with self._lock:
            # 触摸/创建条目（移到末尾以实现 LRU）
            if thread_id in self._history:
                self._history.move_to_end(thread_id)
            else:
                self._history[thread_id] = []
                self._evict_if_needed()

            history = self._history[thread_id]
            history.append(call_hash)
            if len(history) > self.window_size:
                history[:] = history[-self.window_size :]

            count = history.count(call_hash)
            tool_names = [tc.get("name", "?") for tc in tool_calls]

            if count >= self.hard_limit:
                logger.error(
                    "Loop hard limit reached — forcing stop",
                    extra={
                        "thread_id": thread_id,
                        "call_hash": call_hash,
                        "count": count,
                        "tools": tool_names,
                    },
                )
                return _HARD_STOP_MSG, True

            if count >= self.warn_threshold:
                warned = self._warned[thread_id]
                if call_hash not in warned:
                    warned.add(call_hash)
                    logger.warning(
                        "Repetitive tool calls detected — injecting warning",
                        extra={
                            "thread_id": thread_id,
                            "call_hash": call_hash,
                            "count": count,
                            "tools": tool_names,
                        },
                    )
                    return _WARNING_MSG, False
                # 此哈希已注入过警告——抑制
                return None, False

        return None, False

    def _apply(self, state: AgentState, runtime: Runtime) -> dict | None:
        warning, hard_stop = self._track_and_check(state, runtime)

        if hard_stop:
            # 从最后一条 AIMessage 中剥离 tool_calls 以强制输出文本
            messages = state.get("messages", [])
            last_msg = messages[-1]
            stripped_msg = last_msg.model_copy(
                update={
                    "tool_calls": [],
                    "content": (last_msg.content or "") + f"\n\n{_HARD_STOP_MSG}",
                }
            )
            return {"messages": [stripped_msg]}

        if warning:
            # 注入为 HumanMessage 而非 SystemMessage，以避免
            # Anthropic 的"多个非连续系统消息"错误。
            # Anthropic 模型要求系统消息仅在对话开头出现；
            # 在对话中间注入会导致 langchain_anthropic 的
            # _format_messages() 崩溃。HumanMessage 适用于
            # 所有提供者。参见 #1299。
            return {"messages": [HumanMessage(content=warning)]}

        return None

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    def reset(self, thread_id: str | None = None) -> None:
        """清除跟踪状态。如果指定 thread_id，仅清除该线程。"""
        with self._lock:
            if thread_id:
                self._history.pop(thread_id, None)
                self._warned.pop(thread_id, None)
            else:
                self._history.clear()
                self._warned.clear()
