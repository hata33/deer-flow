"""子智能体执行引擎。

提供子智能体的同步/异步执行能力，包括双线程池架构（调度池 + 执行池）、
超时控制、实时消息捕获和后台任务管理。
"""

import asyncio
import logging
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from langchain.agents import create_agent
from langchain.tools import BaseTool
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from deerflow.agents.thread_state import SandboxState, ThreadDataState, ThreadState
from deerflow.models import create_chat_model
from deerflow.subagents.config import SubagentConfig

logger = logging.getLogger(__name__)


class SubagentStatus(Enum):
    """子智能体执行状态。"""

    PENDING = "pending"  # 等待中
    RUNNING = "running"  # 运行中
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"  # 失败
    TIMED_OUT = "timed_out"  # 超时


@dataclass
class SubagentResult:
    """子智能体执行结果。

    Attributes:
        task_id: 本次执行的唯一标识符。
        trace_id: 分布式追踪 ID（关联父智能体和子智能体日志）。
        status: 当前执行状态。
        result: 最终结果消息（如果已完成）。
        error: 错误消息（如果失败）。
        started_at: 执行开始时间。
        completed_at: 执行完成时间。
        ai_messages: 执行过程中生成的完整 AI 消息列表（字典形式）。
    """

    task_id: str
    trace_id: str
    status: SubagentStatus
    result: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    ai_messages: list[dict[str, Any]] | None = None

    def __post_init__(self):
        """初始化可变默认值。"""
        if self.ai_messages is None:
            self.ai_messages = []


# 全局后台任务结果存储
_background_tasks: dict[str, SubagentResult] = {}
_background_tasks_lock = threading.Lock()

# 调度线程池：负责后台任务的调度和编排
_scheduler_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="subagent-scheduler-")

# 执行线程池：负责实际的子智能体执行（支持超时）
# 较大的池以避免调度器提交执行任务时阻塞
_execution_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="subagent-exec-")


def _filter_tools(
    all_tools: list[BaseTool],
    allowed: list[str] | None,
    disallowed: list[str] | None,
) -> list[BaseTool]:
    """根据子智能体配置过滤工具。

    Args:
        all_tools: 所有可用工具列表。
        allowed: 可选的允许列表。如果提供，仅包含这些工具。
        disallowed: 可选的禁止列表。这些工具始终被排除。

    Returns:
        过滤后的工具列表。
    """
    filtered = all_tools

    # 如果指定了允许列表，则应用白名单
    if allowed is not None:
        allowed_set = set(allowed)
        filtered = [t for t in filtered if t.name in allowed_set]

    # 应用禁止列表
    if disallowed is not None:
        disallowed_set = set(disallowed)
        filtered = [t for t in filtered if t.name not in disallowed_set]

    return filtered


def _get_model_name(config: SubagentConfig, parent_model: str | None) -> str | None:
    """解析子智能体使用的模型名称。

    Args:
        config: 子智能体配置。
        parent_model: 父智能体的模型名称。

    Returns:
        要使用的模型名称，或 None 表示使用默认值。
    """
    if config.model == "inherit":
        return parent_model
    return config.model


class SubagentExecutor:
    """子智能体执行器，负责创建和运行子智能体实例。"""

    def __init__(
        self,
        config: SubagentConfig,
        tools: list[BaseTool],
        parent_model: str | None = None,
        sandbox_state: SandboxState | None = None,
        thread_data: ThreadDataState | None = None,
        thread_id: str | None = None,
        trace_id: str | None = None,
    ):
        """初始化执行器。

        Args:
            config: 子智能体配置。
            tools: 所有可用工具列表（将被过滤）。
            parent_model: 父智能体的模型名称，用于继承。
            sandbox_state: 来自父智能体的沙箱状态。
            thread_data: 来自父智能体的线程数据。
            thread_id: 用于沙箱操作的线程 ID。
            trace_id: 来自父智能体的追踪 ID，用于分布式追踪。
        """
        self.config = config
        self.parent_model = parent_model
        self.sandbox_state = sandbox_state
        self.thread_data = thread_data
        self.thread_id = thread_id
        # 如果未提供则生成 trace_id（用于顶层调用）
        self.trace_id = trace_id or str(uuid.uuid4())[:8]

        # 根据配置过滤工具
        self.tools = _filter_tools(
            tools,
            config.tools,
            config.disallowed_tools,
        )

        logger.info(f"[trace={self.trace_id}] SubagentExecutor initialized: {config.name} with {len(self.tools)} tools")

    def _create_agent(self):
        """创建智能体实例。"""
        model_name = _get_model_name(self.config, self.parent_model)
        model = create_chat_model(name=model_name, thinking_enabled=False)

        from deerflow.agents.middlewares.tool_error_handling_middleware import build_subagent_runtime_middlewares

        # 复用与主智能体共享的中间件组合
        middlewares = build_subagent_runtime_middlewares(lazy_init=True)

        return create_agent(
            model=model,
            tools=self.tools,
            middleware=middlewares,
            system_prompt=self.config.system_prompt,
            state_schema=ThreadState,
        )

    def _build_initial_state(self, task: str) -> dict[str, Any]:
        """构建智能体执行的初始状态。

        Args:
            task: 任务描述。

        Returns:
            初始状态字典。
        """
        state: dict[str, Any] = {
            "messages": [HumanMessage(content=task)],
        }

        # 从父智能体传递沙箱和线程数据
        if self.sandbox_state is not None:
            state["sandbox"] = self.sandbox_state
        if self.thread_data is not None:
            state["thread_data"] = self.thread_data

        return state

    async def _aexecute(self, task: str, result_holder: SubagentResult | None = None) -> SubagentResult:
        """异步执行任务。

        Args:
            task: 子智能体的任务描述。
            result_holder: 可选的预创建结果对象，在执行期间更新。

        Returns:
            包含执行结果的 SubagentResult。
        """
        if result_holder is not None:
            # 使用提供的结果持有者（用于带实时更新的异步执行）
            result = result_holder
        else:
            # 为同步执行创建新结果
            task_id = str(uuid.uuid4())[:8]
            result = SubagentResult(
                task_id=task_id,
                trace_id=self.trace_id,
                status=SubagentStatus.RUNNING,
                started_at=datetime.now(),
            )

        try:
            agent = self._create_agent()
            state = self._build_initial_state(task)

            # 构建带有 thread_id 的配置以访问沙箱，并设置递归限制
            run_config: RunnableConfig = {
                "recursion_limit": self.config.max_turns,
            }
            context = {}
            if self.thread_id:
                run_config["configurable"] = {"thread_id": self.thread_id}
                context["thread_id"] = self.thread_id

            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} starting async execution with max_turns={self.config.max_turns}")

            # 使用流式处理而非 invoke 以获取实时更新
            # 这使我们可以在生成时收集 AI 消息
            final_state = None
            async for chunk in agent.astream(state, config=run_config, context=context, stream_mode="values"):  # type: ignore[arg-type]
                final_state = chunk

                # 从当前状态中提取 AI 消息
                messages = chunk.get("messages", [])
                if messages:
                    last_message = messages[-1]
                    # 检查是否为新的 AI 消息
                    if isinstance(last_message, AIMessage):
                        # 将消息转换为字典以便序列化
                        message_dict = last_message.model_dump()
                        # 仅添加不在列表中的消息（避免重复）
                        # 如果有消息 ID 则通过 ID 比较，否则比较完整字典
                        message_id = message_dict.get("id")
                        is_duplicate = False
                        if message_id:
                            is_duplicate = any(msg.get("id") == message_id for msg in result.ai_messages)
                        else:
                            is_duplicate = message_dict in result.ai_messages

                        if not is_duplicate:
                            result.ai_messages.append(message_dict)
                            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} captured AI message #{len(result.ai_messages)}")

            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} completed async execution")

            if final_state is None:
                logger.warning(f"[trace={self.trace_id}] Subagent {self.config.name} no final state")
                result.result = "No response generated"
            else:
                # 提取最终消息 - 查找最后一条 AIMessage
                messages = final_state.get("messages", [])
                logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} final messages count: {len(messages)}")

                # 在对话中查找最后一条 AIMessage
                last_ai_message = None
                for msg in reversed(messages):
                    if isinstance(msg, AIMessage):
                        last_ai_message = msg
                        break

                if last_ai_message is not None:
                    content = last_ai_message.content
                    # 处理字符串和列表两种内容类型的结果
                    if isinstance(content, str):
                        result.result = content
                    elif isinstance(content, list):
                        # 从内容块列表中提取文本（仅用于最终结果）。
                        # 原始字符串片段直接拼接，但完整文本块之间保留分隔以提高可读性。
                        text_parts = []
                        pending_str_parts = []
                        for block in content:
                            if isinstance(block, str):
                                pending_str_parts.append(block)
                            elif isinstance(block, dict):
                                if pending_str_parts:
                                    text_parts.append("".join(pending_str_parts))
                                    pending_str_parts.clear()
                                text_val = block.get("text")
                                if isinstance(text_val, str):
                                    text_parts.append(text_val)
                        if pending_str_parts:
                            text_parts.append("".join(pending_str_parts))
                        result.result = "\n".join(text_parts) if text_parts else "No text content in response"
                    else:
                        result.result = str(content)
                elif messages:
                    # 回退：如果没有找到 AIMessage，使用最后一条消息
                    last_message = messages[-1]
                    logger.warning(f"[trace={self.trace_id}] Subagent {self.config.name} no AIMessage found, using last message: {type(last_message)}")
                    raw_content = last_message.content if hasattr(last_message, "content") else str(last_message)
                    if isinstance(raw_content, str):
                        result.result = raw_content
                    elif isinstance(raw_content, list):
                        parts = []
                        pending_str_parts = []
                        for block in raw_content:
                            if isinstance(block, str):
                                pending_str_parts.append(block)
                            elif isinstance(block, dict):
                                if pending_str_parts:
                                    parts.append("".join(pending_str_parts))
                                    pending_str_parts.clear()
                                text_val = block.get("text")
                                if isinstance(text_val, str):
                                    parts.append(text_val)
                        if pending_str_parts:
                            parts.append("".join(pending_str_parts))
                        result.result = "\n".join(parts) if parts else "No text content in response"
                    else:
                        result.result = str(raw_content)
                else:
                    logger.warning(f"[trace={self.trace_id}] Subagent {self.config.name} no messages in final state")
                    result.result = "No response generated"

            result.status = SubagentStatus.COMPLETED
            result.completed_at = datetime.now()

        except Exception as e:
            logger.exception(f"[trace={self.trace_id}] Subagent {self.config.name} async execution failed")
            result.status = SubagentStatus.FAILED
            result.error = str(e)
            result.completed_at = datetime.now()

        return result

    def execute(self, task: str, result_holder: SubagentResult | None = None) -> SubagentResult:
        """同步执行任务（异步执行的包装器）。

        此方法在新的事件循环中运行异步执行，允许在线程池中使用
        异步工具（如 MCP 工具）。

        Args:
            task: 子智能体的任务描述。
            result_holder: 可选的预创建结果对象，在执行期间更新。

        Returns:
            包含执行结果的 SubagentResult。
        """
        # 在新的事件循环中运行异步执行
        # 这是必要的，因为：
        # 1. 我们可能有仅异步的工具（如 MCP 工具）
        # 2. 我们在线程池中运行，线程池没有事件循环
        #
        # 注意：_aexecute() 内部捕获了所有异常，因此这个外层
        # try-except 仅处理 asyncio.run() 的失败（例如在已存在事件循环的
        # 异步上下文中调用时）。子智能体执行错误在 _aexecute() 中处理，
        # 以 FAILED 状态返回。
        try:
            return asyncio.run(self._aexecute(task, result_holder))
        except Exception as e:
            logger.exception(f"[trace={self.trace_id}] Subagent {self.config.name} execution failed")
            # 如果没有结果对象则创建一个带错误的结果
            if result_holder is not None:
                result = result_holder
            else:
                result = SubagentResult(
                    task_id=str(uuid.uuid4())[:8],
                    trace_id=self.trace_id,
                    status=SubagentStatus.FAILED,
                )
            result.status = SubagentStatus.FAILED
            result.error = str(e)
            result.completed_at = datetime.now()
            return result

    def execute_async(self, task: str, task_id: str | None = None) -> str:
        """在后台启动任务执行。

        Args:
            task: 子智能体的任务描述。
            task_id: 可选的任务 ID。如果未提供，将生成随机 UUID。

        Returns:
            可用于后续查询状态的任务 ID。
        """
        # 使用提供的 task_id 或生成新的
        if task_id is None:
            task_id = str(uuid.uuid4())[:8]

        # 创建初始等待结果
        result = SubagentResult(
            task_id=task_id,
            trace_id=self.trace_id,
            status=SubagentStatus.PENDING,
        )

        logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} starting async execution, task_id={task_id}, timeout={self.config.timeout_seconds}s")

        with _background_tasks_lock:
            _background_tasks[task_id] = result

        # 提交到调度线程池
        def run_task():
            with _background_tasks_lock:
                _background_tasks[task_id].status = SubagentStatus.RUNNING
                _background_tasks[task_id].started_at = datetime.now()
                result_holder = _background_tasks[task_id]

            try:
                # 提交执行到执行池（带超时）
                # 传递 result_holder 以便 execute() 可以实时更新
                execution_future: Future = _execution_pool.submit(self.execute, task, result_holder)
                try:
                    # 等待执行完成（带超时）
                    exec_result = execution_future.result(timeout=self.config.timeout_seconds)
                    with _background_tasks_lock:
                        _background_tasks[task_id].status = exec_result.status
                        _background_tasks[task_id].result = exec_result.result
                        _background_tasks[task_id].error = exec_result.error
                        _background_tasks[task_id].completed_at = datetime.now()
                        _background_tasks[task_id].ai_messages = exec_result.ai_messages
                except FuturesTimeoutError:
                    logger.error(f"[trace={self.trace_id}] Subagent {self.config.name} execution timed out after {self.config.timeout_seconds}s")
                    with _background_tasks_lock:
                        _background_tasks[task_id].status = SubagentStatus.TIMED_OUT
                        _background_tasks[task_id].error = f"Execution timed out after {self.config.timeout_seconds} seconds"
                        _background_tasks[task_id].completed_at = datetime.now()
                    # 取消 future（尽力而为——可能无法停止实际执行）
                    execution_future.cancel()
            except Exception as e:
                logger.exception(f"[trace={self.trace_id}] Subagent {self.config.name} async execution failed")
                with _background_tasks_lock:
                    _background_tasks[task_id].status = SubagentStatus.FAILED
                    _background_tasks[task_id].error = str(e)
                    _background_tasks[task_id].completed_at = datetime.now()

        _scheduler_pool.submit(run_task)
        return task_id


MAX_CONCURRENT_SUBAGENTS = 3  # 最大并发子智能体数


def get_background_task_result(task_id: str) -> SubagentResult | None:
    """获取后台任务的结果。

    Args:
        task_id: execute_async 返回的任务 ID。

    Returns:
        如果找到则返回 SubagentResult，否则返回 None。
    """
    with _background_tasks_lock:
        return _background_tasks.get(task_id)


def list_background_tasks() -> list[SubagentResult]:
    """列出所有后台任务。"""
    with _background_tasks_lock:
        return list(_background_tasks.values())


def cleanup_background_task(task_id: str) -> None:
    """从后台任务中移除已完成的任务。

    应在 task_tool 完成轮询并返回结果后调用，
    以防止累积已完成任务导致内存泄漏。

    仅移除处于终态（COMPLETED/FAILED/TIMED_OUT）的任务，
    以避免与仍在更新任务条目的后台执行器产生竞态条件。

    Args:
        task_id: 要移除的任务 ID。
    """
    with _background_tasks_lock:
        result = _background_tasks.get(task_id)
        if result is None:
            # Nothing to clean up; may have been removed already.
            logger.debug("Requested cleanup for unknown background task %s", task_id)
            return

        # Only clean up tasks that are in a terminal state to avoid races with
        # the background executor still updating the task entry.
        is_terminal_status = result.status in {
            SubagentStatus.COMPLETED,
            SubagentStatus.FAILED,
            SubagentStatus.TIMED_OUT,
        }
        if is_terminal_status or result.completed_at is not None:
            del _background_tasks[task_id]
            logger.debug("Cleaned up background task: %s", task_id)
        else:
            logger.debug(
                "Skipping cleanup for non-terminal background task %s (status=%s)",
                task_id,
                result.status.value if hasattr(result.status, "value") else result.status,
            )
