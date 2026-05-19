"""
通过 LangChain 回调机制捕获运行事件的模块。

RunJournal 位于 LangChain 回调机制和可插拔的 RunEventStore 之间。
它将回调数据标准化为 RunEvent 记录，并处理 token 使用量的累积。

关键设计决策:
- 不实现 on_llm_new_token —— 仅通过 on_llm_end 处理完整消息
- on_chat_model_start 捕获结构化提示作为 llm_request (OpenAI 格式)，
  并提取第一条 human 消息作为 run.input，因为这比 on_chain_start
  （在每个节点触发）更可靠 —— 这里的消息是完全结构化的
- parent_run_id=None 的 on_chain_start 发出 run.start 追踪事件，标记根调用
- on_llm_end 发出 OpenAI Chat Completions 格式的 llm_response
- Token 使用量在内存中累积，在运行完成时写入 RunRow
- 通过 tags 注入识别调用者 (lead_agent / subagent:{name} / middleware:{name})
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.types import Command

if TYPE_CHECKING:
    from deerflow.runtime.events.store.base import RunEventStore

logger = logging.getLogger(__name__)


class RunJournal(BaseCallbackHandler):
    """LangChain 回调处理器，用于将事件捕获到 RunEventStore。

    该类实现了 LangChain 的 BaseCallbackHandler 接口，在运行过程中
    捕获各种事件（LLM 调用、工具调用、链执行等）并将它们标准化
    为 RunEvent 记录存储到事件存储中。
    """

    def __init__(
        self,
        run_id: str,
        thread_id: str,
        event_store: RunEventStore,
        *,
        track_token_usage: bool = True,
        flush_threshold: int = 20,
    ):
        """
        初始化 RunJournal。

        Args:
            run_id: 运行唯一标识符
            thread_id: 线程 ID
            event_store: 事件存储实例
            track_token_usage: 是否跟踪 token 使用量
            flush_threshold: 缓冲区刷新阈值
        """
        super().__init__()
        self.run_id = run_id
        self.thread_id = thread_id
        self._store = event_store
        self._track_tokens = track_token_usage
        self._flush_threshold = flush_threshold

        # 写缓冲区
        self._buffer: list[dict] = []
        self._pending_flush_tasks: set[asyncio.Task[None]] = set()

        # Token 累积器
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_tokens = 0
        self._llm_call_count = 0

        # 按调用者分类的 token 累积器
        self._lead_agent_tokens = 0
        self._subagent_tokens = 0
        self._middleware_tokens = 0

        # 去重: LangChain 可能对同一 run_id 多次触发 on_llm_end
        self._counted_llm_run_ids: set[str] = set()
        self._counted_external_source_ids: set[str] = set()
        self._counted_message_llm_run_ids: set[str] = set()

        # 便捷字段
        self._last_ai_msg: str | None = None
        self._first_human_msg: str | None = None
        self._msg_count = 0

        # 延迟跟踪
        self._llm_start_times: dict[str, float] = {}  # langchain run_id -> 开始时间

        # LLM 请求/响应跟踪
        self._llm_call_index = 0
        self._seen_llm_starts: set[str] = set()  # 触发了 on_chat_model_start 的 langchain run_ids

    # -- 生命周期回调 --

    @staticmethod
    def _message_text(message: BaseMessage) -> str:
        """从消息的混合内容形状中提取可显示的文本。

        Args:
            message: LangChain BaseMessage 对象

        Returns:
            提取的文本内容
        """
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, Mapping):
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                    else:
                        nested = block.get("content")
                        if isinstance(nested, str):
                            parts.append(nested)
            return "".join(parts)
        if isinstance(content, Mapping):
            for key in ("text", "content"):
                value = content.get(key)
                if isinstance(value, str):
                    return value

        text = getattr(message, "text", None)
        if isinstance(text, str):
            return text
        return ""

    def _record_message_summary(self, message: BaseMessage, *, caller: str | None = None) -> None:
        """更新运行级别的便捷字段，用于持久化的运行记录。

        Args:
            message: LangChain BaseMessage 对象
            caller: 调用者标识（可选）
        """
        self._msg_count += 1

        # ``last_ai_message`` 应该代表 lead agent 的面向用户的回答。
        # Middleware/subagent 模型调用和空的仅工具调用的 AI 消息
        # 不应覆盖最后有用的助手文本。
        is_ai_message = isinstance(message, AIMessage) or getattr(message, "type", None) == "ai"
        if is_ai_message and (caller is None or caller == "lead_agent"):
            text = self._message_text(message).strip()
            if text:
                self._last_ai_msg = text[:2000]

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """链开始时的回调处理。

        Args:
            serialized: 序列化的链信息
            inputs: 输入数据
            run_id: 运行 ID
            parent_run_id: 父运行 ID
            tags: 标签列表
            metadata: 元数据
        """
        caller = self._identify_caller(tags)
        if parent_run_id is None:
            # 根图调用 —— 为运行开始发出单个追踪事件
            chain_name = (serialized or {}).get("name", "unknown")
            self._put(
                event_type="run.start",
                category="trace",
                content={"chain": chain_name},
                metadata={"caller": caller, **(metadata or {})},
            )

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """链结束时的回调处理。"""
        self._put(event_type="run.end", category="outputs", content=outputs, metadata={"status": "success"})
        self._flush_sync()

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """链错误时的回调处理。"""
        self._put(
            event_type="run.error",
            category="error",
            content=str(error),
            metadata={"error_type": type(error).__name__},
        )
        self._flush_sync()

    # -- LLM 回调 --

    def on_chat_model_start(
        self,
        serialized: dict,
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """捕获结构化提示消息用于 llm_request 事件。

        这也是提取第一条 human message 的规范位置：
        这里的消息是完全结构化的，仅在真实的 LLM 调用时触发，
        且内容永远不会被检查点修剪压缩。

        Args:
            serialized: 序列化的模型信息
            messages: 消息批次列表
            run_id: 运行 ID
            tags: 标签列表
        """
        rid = str(run_id)
        self._llm_start_times[rid] = time.monotonic()
        self._llm_call_index += 1
        self._seen_llm_starts.add(rid)

        logger.debug(
            "on_chat_model_start %s: tags=%s num_batches=%d message_counts=%s",
            run_id,
            tags,
            len(messages),
            [len(batch) for batch in messages],
        )

        # 捕获发送给此运行中任何 LLM 的第一条 human 消息
        if not self._first_human_msg and messages:
            for batch in reversed(messages):
                for m in reversed(batch):
                    if isinstance(m, HumanMessage) and m.name != "summary":
                        caller = self._identify_caller(tags)
                        self.set_first_human_message(m.text)
                        self._put(
                            event_type="llm.human.input",
                            category="message",
                            content=m.model_dump(),
                            metadata={"caller": caller},
                        )
                        self._record_message_summary(m, caller=caller)
                        break
                if self._first_human_msg:
                    break

    def on_llm_start(self, serialized: dict, prompts: list[str], *, run_id: UUID, parent_run_id: UUID | None = None, tags: list[str] | None = None, metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
        """LLM 开始回调（备用方法）。

        注意: on_chat_model_start 是首选方法。这里仅跟踪延迟。
        """
        self._llm_start_times[str(run_id)] = time.monotonic()

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """LLM 结束回调处理。

        Args:
            response: LLM 响应对象
            run_id: 运行 ID
            parent_run_id: 父运行 ID
            tags: 标签列表
        """
        messages: list[AnyMessage] = []
        logger.debug("on_llm_end %s: tags=%s", run_id, tags)
        for generation in response.generations:
            for gen in generation:
                if hasattr(gen, "message"):
                    messages.append(gen.message)
                else:
                    logger.warning(f"on_llm_end {run_id}: generation has no message attribute: {gen}")

        for message in messages:
            caller = self._identify_caller(tags)

            # 计算延迟
            rid = str(run_id)
            start = self._llm_start_times.pop(rid, None)
            latency_ms = int((time.monotonic() - start) * 1000) if start else None

            # 从消息中提取 token 使用量
            usage = getattr(message, "usage_metadata", None)
            usage_dict = dict(usage) if usage else {}

            # 解析调用索引
            call_index = self._llm_call_index
            if rid not in self._seen_llm_starts:
                # 备用: on_chat_model_start 未被调用
                self._llm_call_index += 1
                call_index = self._llm_call_index
                self._seen_llm_starts.add(rid)

            # 追踪事件: llm_response (OpenAI completion 格式)
            self._put(
                event_type="llm.ai.response",
                category="message",
                content=message.model_dump(),
                metadata={
                    "caller": caller,
                    "usage": usage_dict,
                    "latency_ms": latency_ms,
                    "llm_call_index": call_index,
                },
            )
            if rid not in self._counted_message_llm_run_ids:
                self._record_message_summary(message, caller=caller)

            # Token 累积（通过 langchain run_id 去重，避免同一响应的多次回调重复计数）
            if self._track_tokens:
                input_tk = usage_dict.get("input_tokens", 0) or 0
                output_tk = usage_dict.get("output_tokens", 0) or 0
                total_tk = usage_dict.get("total_tokens", 0) or 0
                if total_tk == 0:
                    total_tk = input_tk + output_tk
                if total_tk > 0 and rid not in self._counted_llm_run_ids:
                    self._counted_llm_run_ids.add(rid)
                    self._total_input_tokens += input_tk
                    self._total_output_tokens += output_tk
                    self._total_tokens += total_tk
                    self._llm_call_count += 1

                    # 按调用者分类累积 token
                    if caller.startswith("subagent:"):
                        self._subagent_tokens += total_tk
                    elif caller.startswith("middleware:"):
                        self._middleware_tokens += total_tk
                    else:
                        self._lead_agent_tokens += total_tk

        if messages:
            self._counted_message_llm_run_ids.add(str(run_id))

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """LLM 错误回调处理。"""
        self._llm_start_times.pop(str(run_id), None)
        self._put(event_type="llm.error", category="trace", content=str(error))

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, tags=None, metadata=None, inputs=None, **kwargs):
        """处理工具开始事件，缓存工具调用 ID 以便后续关联。

        Args:
            serialized: 序列化的工具信息
            input_str: 输入字符串
            run_id: 运行 ID
            parent_run_id: 父运行 ID
            tags: 标签列表
            metadata: 元数据
            inputs: 输入数据
        """
        tool_call_id = str(run_id)
        logger.debug("Tool start for node %s, tool_call_id=%s, tags=%s", run_id, tool_call_id, tags)

    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
        """处理工具结束事件，追加消息并清除节点数据。

        Args:
            output: 工具输出
            run_id: 运行 ID
            parent_run_id: 父运行 ID
        """
        try:
            if isinstance(output, ToolMessage):
                msg = cast(ToolMessage, output)
                self._put(event_type="llm.tool.result", category="message", content=msg.model_dump())
                self._record_message_summary(msg)
            elif isinstance(output, Command):
                cmd = cast(Command, output)
                messages = cmd.update.get("messages", [])
                for message in messages:
                    if isinstance(message, BaseMessage):
                        self._put(event_type="llm.tool.result", category="message", content=message.model_dump())
                        self._record_message_summary(message)
                    else:
                        logger.warning(f"on_tool_end {run_id}: command update message is not BaseMessage: {type(message)}")
            else:
                logger.warning(f"on_tool_end {run_id}: output is not ToolMessage: {type(output)}")
        finally:
            logger.debug("Tool end for node %s", run_id)

    # -- 内部方法 --

    def _put(self, *, event_type: str, category: str, content: str | dict = "", metadata: dict | None = None) -> None:
        """将事件放入缓冲区。

        Args:
            event_type: 事件类型
            category: 事件类别
            content: 事件内容
            metadata: 元数据
        """
        self._buffer.append(
            {
                "thread_id": self.thread_id,
                "run_id": self.run_id,
                "event_type": event_type,
                "category": category,
                "content": content,
                "metadata": metadata or {},
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        if len(self._buffer) >= self._flush_threshold:
            self._flush_sync()

    def _flush_sync(self) -> None:
        """尽最大努力将缓冲区刷新到 RunEventStore。

        BaseCallbackHandler 方法是同步的。如果事件循环正在运行，
        我们调度一个异步 ``put_batch``；否则事件保留在缓冲区中，
        稍后由 worker 的 ``finally`` 块中的异步 ``flush()`` 调用刷新。
        """
        if not self._buffer:
            return
        # 如果刷新正在进行则跳过 —— 避免多个即发即弃任务
        # 对同一 SQLite 文件的并发写入
        if self._pending_flush_tasks:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有事件循环 —— 将事件保留在缓冲区中供稍后异步刷新
            return
        batch = self._buffer.copy()
        self._buffer.clear()
        task = loop.create_task(self._flush_async(batch))
        self._pending_flush_tasks.add(task)
        task.add_done_callback(self._on_flush_done)

    async def _flush_async(self, batch: list[dict]) -> None:
        """异步刷新事件批次到存储。

        Args:
            batch: 要刷新的事件批次
        """
        try:
            await self._store.put_batch(batch)
        except Exception:
            logger.warning(
                "Failed to flush %d events for run %s — returning to buffer",
                len(batch),
                self.run_id,
                exc_info=True,
            )
            # 将失败的事件返回缓冲区，下次刷新时重试
            self._buffer = batch + self._buffer

    def _on_flush_done(self, task: asyncio.Task) -> None:
        """刷新任务完成回调。

        Args:
            task: 完成的异步任务
        """
        self._pending_flush_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning("Journal flush task failed: %s", exc)

    def _identify_caller(self, tags: list[str] | None) -> str:
        """从标签中识别调用者。

        Args:
            tags: 标签列表

        Returns:
            调用者标识字符串
        """
        _tags = tags or []
        for tag in _tags:
            if isinstance(tag, str) and (tag.startswith("subagent:") or tag.startswith("middleware:") or tag == "lead_agent"):
                return tag
        # 默认为 lead_agent: 主 agent 图不注入回调标签，
        # 而 subagents 和 middleware 显式标记自己
        return "lead_agent"

    # -- 公共方法（由 worker 调用）--

    def record_external_llm_usage_records(
        self,
        records: list[dict[str, int | str]],
    ) -> None:
        """记录来自外部源（如 subagents）的 token 使用量。

        每条记录应包含:
            source_run_id: 唯一标识符，防止重复计数
            caller: 调用者标签（如 "subagent:general-purpose"）
            input_tokens: 输入 token 数量
            output_tokens: 输出 token 数量
            total_tokens: 总 token 数量（如果为 0/缺失则从 input+output 计算）

        Args:
            records: token 使用记录列表
        """
        if not self._track_tokens:
            return
        for record in records:
            source_id = str(record.get("source_run_id", ""))
            if not source_id:
                continue
            if source_id in self._counted_external_source_ids:
                continue

            total_tk = record.get("total_tokens", 0) or 0
            if total_tk <= 0:
                input_tk = record.get("input_tokens", 0) or 0
                output_tk = record.get("output_tokens", 0) or 0
                total_tk = input_tk + output_tk
            if total_tk <= 0:
                continue

            self._counted_external_source_ids.add(source_id)
            self._total_input_tokens += record.get("input_tokens", 0) or 0
            self._total_output_tokens += record.get("output_tokens", 0) or 0
            self._total_tokens += total_tk

            # 按调用者分类累积 token
            caller = str(record.get("caller", ""))
            if caller.startswith("subagent:"):
                self._subagent_tokens += total_tk
            elif caller.startswith("middleware:"):
                self._middleware_tokens += total_tk
            else:
                self._lead_agent_tokens += total_tk

    def set_first_human_message(self, content: str) -> None:
        """记录第一条 human 消息用于便捷字段。

        Args:
            content: 消息内容
        """
        self._first_human_msg = content[:2000] if content else None

    def record_middleware(self, tag: str, *, name: str, hook: str, action: str, changes: dict) -> None:
        """记录 middleware 状态变更事件。

        当 middleware 实现执行有意义的状态变更时调用
        （如标题生成、摘要、HITL 批准）。纯观察的 middleware 不应调用此方法。

        Args:
            tag: middleware 的简短标识符（如 "title"、"summarize"、"guardrail"）。
                 用于形成 event_type="middleware:{tag}"。
            name: 完整的 middleware 类名。
            hook: 触发操作的生命周期钩子（如 "after_model"）。
            action: 执行的特定操作（如 "generate_title"）。
            changes: 描述所做状态更改的字典。
        """
        self._put(
            event_type=f"middleware:{tag}",
            category="middleware",
            content={"name": name, "hook": hook, "action": action, "changes": changes},
        )

    async def flush(self) -> None:
        """强制刷新剩余缓冲区。在 worker 的 finally 块中调用。"""
        if self._pending_flush_tasks:
            await asyncio.gather(*tuple(self._pending_flush_tasks), return_exceptions=True)

        while self._buffer:
            batch = self._buffer[: self._flush_threshold]
            del self._buffer[: self._flush_threshold]
            try:
                await self._store.put_batch(batch)
            except Exception:
                self._buffer = batch + self._buffer
                raise

    def get_completion_data(self) -> dict:
        """返回累积的 token 和消息数据，用于运行完成。

        Returns:
            包含完成数据的字典
        """
        return {
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_tokens": self._total_tokens,
            "llm_call_count": self._llm_call_count,
            "lead_agent_tokens": self._lead_agent_tokens,
            "subagent_tokens": self._subagent_tokens,
            "middleware_tokens": self._middleware_tokens,
            "message_count": self._msg_count,
            "last_ai_message": self._last_ai_msg,
            "first_human_message": self._first_human_msg,
        }
