"""
摘要前记忆刷入钩子（第 4 层辅助模块）

本模块提供 SummarizationMiddleware 的记忆刷入钩子。

为什么需要这个钩子：
  SummarizationMiddleware 在对话接近 token 限制时会丢弃旧消息并保留摘要。
  如果在丢弃之前没有提取记忆，那些消息中的用户信息就会永久丢失。
  memory_flush_hook() 在消息被丢弃前将其刷入记忆队列，
  使用 add_nowait()（无防抖等待）确保立即处理。

与标准防抖入口的区别：
  - 标准入口：MemoryMiddleware.after_agent() → queue.add()（30s 防抖）
  - 本钩子：SummarizationMiddleware 即将丢弃消息 → queue.add_nowait()（立即处理）

信号检测：
  在刷入前对过滤后的消息执行纠错/正面反馈检测：
  - correction_detected：纠错信号优先级高于正面反馈
  - reinforcement_detected：仅在无纠错信号时检测

依赖关系：
  - message_processing.py：filter_messages_for_memory()、detect_correction()、detect_reinforcement()
  - queue.py：get_memory_queue()、add_nowait()
  - summarization_middleware.py：SummarizationEvent 事件定义
  - memory_config.py：enabled 配置检查
  - user_context.py：resolve_runtime_user_id() 解析用户 ID
"""

from __future__ import annotations

from deerflow.agents.memory.message_processing import detect_correction, detect_reinforcement, filter_messages_for_memory
from deerflow.agents.memory.queue import get_memory_queue
from deerflow.agents.middlewares.summarization_middleware import SummarizationEvent
from deerflow.config.memory_config import get_memory_config
from deerflow.runtime.user_context import resolve_runtime_user_id


def memory_flush_hook(event: SummarizationEvent) -> None:
    """将即将被摘要丢弃的消息刷入记忆队列。

    由 SummarizationMiddleware 在丢弃消息之前调用。

    处理流程：
    1. 检查记忆功能是否启用、thread_id 是否有效
    2. 过滤消息（只保留 human 和无 tool_calls 的 AI 消息）
    3. 检查是否同时存在用户和助手消息（单方面消息无需更新记忆）
    4. 检测纠错/正面反馈信号
    5. 解析 user_id（从 runtime 上下文）
    6. 使用 add_nowait() 立即排入队列（不等待防抖）

    Args:
        event: SummarizationEvent，包含 messages_to_summarize、thread_id、agent_name、runtime 等信息
    """
    if not get_memory_config().enabled or not event.thread_id:
        return

    # 过滤消息：只保留对记忆更新有用的内容
    filtered_messages = filter_messages_for_memory(list(event.messages_to_summarize))
    # 必须同时有用户和助手消息才有分析价值
    user_messages = [message for message in filtered_messages if getattr(message, "type", None) == "human"]
    assistant_messages = [message for message in filtered_messages if getattr(message, "type", None) == "ai"]
    if not user_messages or not assistant_messages:
        return

    # 信号检测：纠错优先于正面反馈
    correction_detected = detect_correction(filtered_messages)
    reinforcement_detected = not correction_detected and detect_reinforcement(filtered_messages)
    # 从 runtime 上下文解析 user_id（不依赖 ContextVar，因为可能在任意线程）
    user_id = resolve_runtime_user_id(event.runtime)
    # 立即排入队列（无防抖等待），确保在消息被丢弃前完成记忆提取
    queue = get_memory_queue()
    queue.add_nowait(
        thread_id=event.thread_id,
        messages=filtered_messages,
        agent_name=event.agent_name,
        user_id=user_id,
        correction_detected=correction_detected,
        reinforcement_detected=reinforcement_detected,
    )
