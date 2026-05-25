"""子代理任务委派工具（Task Delegation Tool）

本模块实现了 `task` 工具，允许主代理将复杂任务委派给专门的子代理执行。

核心机制：
--------
1. **异步后台执行**：子代理任务在独立的后台线程中异步执行
2. **轮询等待**：主代理在工具调用中轮询子代理状态，直到完成或超时
3. **实时流式更新**：通过 LangGraph stream writer 发送子代理的进度事件
4. **取消处理**：支持通过 asyncio.CancelledError 协作式取消子代理任务
5. **令牌使用追踪**：记录子代理的令牌使用量并报告给父代理

子代理类型：
----------
- **general-purpose**：通用代理，用于需要复杂推理和多步骤的复杂任务
- **bash**：命令执行专家，仅在 host bash 被允许或使用隔离沙箱时可用
- **自定义类型**：通过 config.yaml 的 `subagents.custom_agents` 配置

防嵌套设计：
----------
子代理内部不会再次加载子代理工具（subagent_enabled=False），
防止递归嵌套导致的无限调用链。

令牌使用报告：
------------
子代理的令牌使用量通过以下机制追踪：
1. 子代理执行完成后，使用量记录缓存在 `_subagent_usage_cache` 中
2. `pop_cached_subagent_usage()` 供 TokenUsageMiddleware 读取
3. 使用量也会通过 `record_external_llm_usage_records` 报告给父代理的 RunJournal

取消与清理流程：
--------------
1. 主代理收到 CancelledError
2. 调用 `request_cancel_background_task()` 请求协作式取消
3. 使用 `asyncio.shield` 等待子代理达到终态（确保令牌使用量快照完整）
4. 如果子代理未在轮询限制内终止，安排延迟清理任务
5. 延迟清理任务持续轮询直到可以安全移除

流式事件类型：
------------
- task_started：任务开始
- task_running：子代理产生新的 AI 消息
- task_completed：任务成功完成
- task_failed：任务失败
- task_cancelled：任务被取消
- task_timed_out：任务超时
"""

import asyncio
import logging
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, Any, cast

from langchain.tools import InjectedToolCallId, tool
from langgraph.config import get_stream_writer

from deerflow.config import get_app_config
from deerflow.sandbox.security import LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE, is_host_bash_allowed
from deerflow.subagents import SubagentExecutor, get_available_subagent_names, get_subagent_config
from deerflow.subagents.config import resolve_subagent_model_name
from deerflow.subagents.executor import (
    SubagentStatus,
    cleanup_background_task,
    get_background_task_result,
    request_cancel_background_task,
)
from deerflow.tools.types import Runtime

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

# 按 tool_call_id 缓存子代理令牌使用量
# 供 TokenUsageMiddleware 在 AIMessage 的 usage_metadata 中写回
_subagent_usage_cache: dict[str, dict[str, int]] = {}


def _token_usage_cache_enabled(app_config: "AppConfig | None") -> bool:
    """检查令牌使用追踪是否已启用。

    查找 app_config 中的 token_usage.enabled 配置。
    如果 app_config 未提供，尝试从全局配置获取。
    """
    if app_config is None:
        try:
            app_config = get_app_config()
        except FileNotFoundError:
            return False
    return bool(getattr(getattr(app_config, "token_usage", None), "enabled", False))


def _cache_subagent_usage(tool_call_id: str, usage: dict | None, *, enabled: bool = True) -> None:
    """缓存子代理的令牌使用量（如果追踪已启用且有使用数据）。"""
    if enabled and usage:
        _subagent_usage_cache[tool_call_id] = usage


def pop_cached_subagent_usage(tool_call_id: str) -> dict | None:
    """弹出并返回缓存的子代理令牌使用量。

    供 TokenUsageMiddleware 在处理工具调用结果时调用，
    将子代理的令牌使用量写回到触发该工具调用的 AIMessage 的 usage_metadata 中。
    """
    return _subagent_usage_cache.pop(tool_call_id, None)


def _is_subagent_terminal(result: Any) -> bool:
    """判断后台子代理结果是否处于终态（可以安全清理）。

    终态包括：COMPLETED、FAILED、CANCELLED、TIMED_OUT，
    或者已经设置了 completed_at 时间戳。
    """
    return result.status in {SubagentStatus.COMPLETED, SubagentStatus.FAILED, SubagentStatus.CANCELLED, SubagentStatus.TIMED_OUT} or getattr(result, "completed_at", None) is not None


async def _await_subagent_terminal(task_id: str, max_polls: int) -> Any | None:
    """轮询直到后台子代理达到终态或用完轮询次数。

    用于 CancelledError 处理中，确保在父代理清理之前
    获取子代理的最终令牌使用量快照。
    """
    for _ in range(max_polls):
        result = get_background_task_result(task_id)
        if result is None:
            return None
        if _is_subagent_terminal(result):
            return result
        await asyncio.sleep(5)
    return None


async def _deferred_cleanup_subagent_task(task_id: str, trace_id: str, max_polls: int) -> None:
    """持续轮询已取消的子代理直到可以安全移除。

    当子代理在取消后仍未达到终态时，此异步任务会在后台持续轮询，
    直到子代理完成或轮询次数耗尽。
    """
    cleanup_poll_count = 0
    while True:
        result = get_background_task_result(task_id)
        if result is None:
            return
        if _is_subagent_terminal(result):
            cleanup_background_task(task_id)
            return
        if cleanup_poll_count >= max_polls:
            logger.warning(f"[trace={trace_id}] Deferred cleanup for task {task_id} timed out after {cleanup_poll_count} polls")
            return
        await asyncio.sleep(5)
        cleanup_poll_count += 1


def _log_cleanup_failure(cleanup_task: asyncio.Task[None], *, trace_id: str, task_id: str) -> None:
    """延迟清理任务的完成回调，记录清理失败时的异常信息。"""
    if cleanup_task.cancelled():
        return

    exc = cleanup_task.exception()
    if exc is not None:
        logger.error(f"[trace={trace_id}] Deferred cleanup failed for task {task_id}: {exc}")


def _schedule_deferred_subagent_cleanup(task_id: str, trace_id: str, max_polls: int) -> None:
    """安排子代理的延迟清理任务。

    当子代理在取消操作后仍未达到终态时，创建一个后台 asyncio.Task
    持续轮询，直到可以安全清理子代理。
    """
    logger.debug(f"[trace={trace_id}] Scheduling deferred cleanup for cancelled task {task_id}")
    cleanup_task = asyncio.create_task(_deferred_cleanup_subagent_task(task_id, trace_id, max_polls))
    cleanup_task.add_done_callback(lambda task: _log_cleanup_failure(task, trace_id=trace_id, task_id=task_id))


def _find_usage_recorder(runtime: Any) -> Any | None:
    """在运行时配置的回调列表中查找具有 `record_external_llm_usage_records` 方法的处理器。

    用于将子代理的令牌使用量报告给父代理的 RunJournal。
    """
    if runtime is None:
        return None
    config = getattr(runtime, "config", None)
    if not isinstance(config, dict):
        return None
    callbacks = config.get("callbacks", [])
    if not callbacks:
        return None
    for cb in callbacks:
        if hasattr(cb, "record_external_llm_usage_records"):
            return cb
    return None


def _summarize_usage(records: list[dict] | None) -> dict | None:
    """将令牌使用记录汇总为紧凑的字典格式，用于 SSE 事件。

    汇总 input_tokens、output_tokens 和 total_tokens 的总和。
    """
    if not records:
        return None
    return {
        "input_tokens": sum(r.get("input_tokens", 0) or 0 for r in records),
        "output_tokens": sum(r.get("output_tokens", 0) or 0 for r in records),
        "total_tokens": sum(r.get("total_tokens", 0) or 0 for r in records),
    }


def _report_subagent_usage(runtime: Any, result: Any) -> None:
    """向父代理的 RunJournal 报告子代理的令牌使用量。

    每个子代理任务只报告一次（通过 usage_reported 标志守卫）。
    如果运行时回调中没有 usage recorder，则跳过报告。
    """
    if getattr(result, "usage_reported", True):
        return
    records = getattr(result, "token_usage_records", None) or []
    if not records:
        return
    journal = _find_usage_recorder(runtime)
    if journal is None:
        logger.debug("No usage recorder found in runtime callbacks — subagent token usage not recorded")
        return
    try:
        journal.record_external_llm_usage_records(records)
        result.usage_reported = True
    except Exception:
        logger.warning("Failed to report subagent token usage", exc_info=True)


def _get_runtime_app_config(runtime: Any) -> "AppConfig | None":
    """从运行时上下文中提取 AppConfig 实例。

    优先从 runtime.context["app_config"] 获取。
    """
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        app_config = context.get("app_config")
        if app_config is not None:
            return cast("AppConfig", app_config)
    return None


def _merge_skill_allowlists(parent: list[str] | None, child: list[str] | None) -> list[str] | None:
    """合并父代理和子代理的技能白名单。

    合并策略：
    - 父级为 None（无限制）：使用子级的白名单
    - 子级为 None（无限制）：继承父级的白名单
    - 两者都有值：取交集（子级只能在父级允许的范围内选择）
    """
    if parent is None:
        return child
    if child is None:
        return list(parent)

    parent_set = set(parent)
    return [skill for skill in child if skill in parent_set]


@tool("task", parse_docstring=True)
async def task_tool(
    runtime: Runtime,
    description: str,
    prompt: str,
    subagent_type: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Delegate a task to a specialized subagent that runs in its own context.

    将任务委派给在独立上下文中运行的专门子代理。

    子代理帮助您：
    - 通过将探索和实现分开来保持上下文清晰
    - 自主处理复杂的多步骤任务
    - 在隔离的上下文中执行命令或操作

    内置子代理类型：
    - **general-purpose**：功能强大的通用代理，用于需要复杂推理、多步骤依赖或
      受益于隔离上下文的复杂多步骤任务。
    - **bash**：命令执行专家，用于运行 bash 命令。仅在 host bash
      被明确允许或使用隔离 shell 沙箱（如 `AioSandboxProvider`）时可用。

    其他自定义子代理类型可以在 config.yaml 的 `subagents.custom_agents` 中定义。
    每个自定义类型可以有自己的系统提示、工具、技能、模型和超时配置。
    如果提供了未知的 subagent_type，错误消息将列出所有可用类型。

    何时使用此工具：
    - 需要多个步骤或工具的复杂任务
    - 产生冗长输出的任务
    - 需要将上下文与主对话隔离时
    - 并行研究或探索任务

    何时不应使用此工具：
    - 简单的单步操作（直接使用工具）
    - 需要用户交互或澄清的任务

    Args:
        description: 任务的简短（3-5 个词）描述，用于日志/显示。始终首先提供此参数。
        prompt: 子代理的任务描述。要具体、明确。始终第二个提供此参数。
        subagent_type: 要使用的子代理类型。始终第三个提供此参数。
    """
    runtime_app_config = _get_runtime_app_config(runtime)
    cache_token_usage = _token_usage_cache_enabled(runtime_app_config)

    # 获取可用的子代理名称列表
    available_subagent_names = get_available_subagent_names(app_config=runtime_app_config) if runtime_app_config is not None else get_available_subagent_names()

    # 获取子代理配置
    config = get_subagent_config(subagent_type, app_config=runtime_app_config) if runtime_app_config is not None else get_subagent_config(subagent_type)
    if config is None:
        available = ", ".join(available_subagent_names)
        return f"Error: Unknown subagent type '{subagent_type}'. Available: {available}"

    # bash 类型子代理需要检查 host bash 是否被允许
    if subagent_type == "bash":
        host_bash_allowed = is_host_bash_allowed(runtime_app_config) if runtime_app_config is not None else is_host_bash_allowed()
        if not host_bash_allowed:
            return f"Error: {LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE}"

    # 构建配置覆盖项
    overrides: dict = {}

    # 技能由 SubagentExecutor 按会话加载（与 Codex 的模式一致：
    # 每个子代理根据自己的配置加载技能，作为对话项注入）。
    # 不再在此处追加到 system_prompt。

    # 从运行时中提取父代理上下文
    sandbox_state = None
    thread_data = None
    thread_id = None
    parent_model = None
    trace_id = None
    metadata: dict = {}

    if runtime is not None:
        sandbox_state = runtime.state.get("sandbox")
        thread_data = runtime.state.get("thread_data")
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if thread_id is None:
            thread_id = runtime.config.get("configurable", {}).get("thread_id")

        # 尝试从 configurable 获取父代理模型
        metadata = runtime.config.get("metadata", {})
        parent_model = metadata.get("model_name")

        # 获取或生成 trace_id 用于分布式追踪
        trace_id = metadata.get("trace_id") or str(uuid.uuid4())[:8]

    # 合并技能白名单：子代理只能在父代理允许的范围内选择
    parent_available_skills = metadata.get("available_skills")
    if parent_available_skills is not None:
        overrides["skills"] = _merge_skill_allowlists(list(parent_available_skills), config.skills)

    if overrides:
        config = replace(config, **overrides)

    # 获取可用工具（排除 task 工具以防止嵌套）
    # 延迟导入以避免循环依赖
    from deerflow.tools import get_available_tools

    # 继承父代理的 tool_groups，使子代理遵守相同的限制
    parent_tool_groups = metadata.get("tool_groups")
    resolved_app_config = runtime_app_config
    if config.model == "inherit" and parent_model is None and resolved_app_config is None:
        resolved_app_config = get_app_config()
    effective_model = resolve_subagent_model_name(config, parent_model, app_config=resolved_app_config)

    # 子代理不应启用子代理工具（防止递归嵌套）
    available_tools_kwargs = {
        "model_name": effective_model,
        "groups": parent_tool_groups,
        "subagent_enabled": False,  # 关键：防止递归嵌套
    }
    if resolved_app_config is not None:
        available_tools_kwargs["app_config"] = resolved_app_config
    tools = get_available_tools(**available_tools_kwargs)

    # 创建子代理执行器
    executor_kwargs = {
        "config": config,
        "tools": tools,
        "parent_model": parent_model,
        "sandbox_state": sandbox_state,
        "thread_data": thread_data,
        "thread_id": thread_id,
        "trace_id": trace_id,
    }
    if resolved_app_config is not None:
        executor_kwargs["app_config"] = resolved_app_config
    executor = SubagentExecutor(**executor_kwargs)

    # 启动后台执行（始终异步，防止阻塞）
    # 使用 tool_call_id 作为 task_id 以便追踪关联
    task_id = executor.execute_async(prompt, task_id=tool_call_id)

    # 在后端轮询等待任务完成（移除了 LLM 轮询的需求）
    poll_count = 0
    last_status = None
    last_message_count = 0  # 追踪已发送的 AI 消息数量

    # 轮询超时：执行超时 + 60 秒缓冲，每 5 秒检查一次
    max_poll_count = (config.timeout_seconds + 60) // 5

    logger.info(f"[trace={trace_id}] Started background task {task_id} (subagent={subagent_type}, timeout={config.timeout_seconds}s, polling_limit={max_poll_count} polls)")

    writer = get_stream_writer()
    # 发送任务开始事件
    writer({"type": "task_started", "task_id": task_id, "description": description})

    try:
        while True:
            result = get_background_task_result(task_id)

            if result is None:
                logger.error(f"[trace={trace_id}] Task {task_id} not found in background tasks")
                writer({"type": "task_failed", "task_id": task_id, "error": "Task disappeared from background tasks"})
                cleanup_background_task(task_id)
                return f"Error: Task {task_id} disappeared from background tasks"

            # 记录状态变化用于调试
            if result.status != last_status:
                logger.info(f"[trace={trace_id}] Task {task_id} status: {result.status.value}")
                last_status = result.status

            # 检查新的 AI 消息并发送 task_running 事件
            ai_messages = result.ai_messages or []
            current_message_count = len(ai_messages)
            if current_message_count > last_message_count:
                # 为每条新消息发送 task_running 事件
                for i in range(last_message_count, current_message_count):
                    message = ai_messages[i]
                    writer(
                        {
                            "type": "task_running",
                            "task_id": task_id,
                            "message": message,
                            "message_index": i + 1,  # 基于 1 的索引用于显示
                            "total_messages": current_message_count,
                        }
                    )
                    logger.info(f"[trace={trace_id}] Task {task_id} sent message #{i + 1}/{current_message_count}")
                last_message_count = current_message_count

            # 检查任务是否完成、失败或超时
            usage = _summarize_usage(getattr(result, "token_usage_records", None))
            if result.status == SubagentStatus.COMPLETED:
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                _report_subagent_usage(runtime, result)
                writer({"type": "task_completed", "task_id": task_id, "result": result.result, "usage": usage})
                logger.info(f"[trace={trace_id}] Task {task_id} completed after {poll_count} polls")
                cleanup_background_task(task_id)
                return f"Task Succeeded. Result: {result.result}"
            elif result.status == SubagentStatus.FAILED:
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                _report_subagent_usage(runtime, result)
                writer({"type": "task_failed", "task_id": task_id, "error": result.error, "usage": usage})
                logger.error(f"[trace={trace_id}] Task {task_id} failed: {result.error}")
                cleanup_background_task(task_id)
                return f"Task failed. Error: {result.error}"
            elif result.status == SubagentStatus.CANCELLED:
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                _report_subagent_usage(runtime, result)
                writer({"type": "task_cancelled", "task_id": task_id, "error": result.error, "usage": usage})
                logger.info(f"[trace={trace_id}] Task {task_id} cancelled: {result.error}")
                cleanup_background_task(task_id)
                return "Task cancelled by user."
            elif result.status == SubagentStatus.TIMED_OUT:
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                _report_subagent_usage(runtime, result)
                writer({"type": "task_timed_out", "task_id": task_id, "error": result.error, "usage": usage})
                logger.warning(f"[trace={trace_id}] Task {task_id} timed out: {result.error}")
                cleanup_background_task(task_id)
                return f"Task timed out. Error: {result.error}"

            # 仍在运行，等待下一次轮询
            await asyncio.sleep(5)
            poll_count += 1

            # 轮询超时作为安全网（防止线程池超时不生效的情况）
            # 设置为执行超时 + 60 秒缓冲，按 5 秒轮询间隔计算
            # 这捕获了后台任务卡住的边缘情况
            # 注意：此处不调用 cleanup_background_task，因为任务可能
            # 仍在后台运行。清理将在执行器完成并设置终态时发生。
            if poll_count > max_poll_count:
                timeout_minutes = config.timeout_seconds // 60
                logger.error(f"[trace={trace_id}] Task {task_id} polling timed out after {poll_count} polls (should have been caught by thread pool timeout)")
                _report_subagent_usage(runtime, result)
                usage = _summarize_usage(getattr(result, "token_usage_records", None))
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                writer({"type": "task_timed_out", "task_id": task_id, "usage": usage})
                return f"Task polling timed out after {timeout_minutes} minutes. This may indicate the background task is stuck. Status: {result.status.value}"
    except asyncio.CancelledError:
        # 向后台子代理线程发送协作式停止信号
        request_cancel_background_task(task_id)

        # （被 shield 保护）等待子代理达到终态，以便在
        # 父代理 worker 持久化 get_completion_data() 之前
        # 报告最终的令牌使用量快照
        terminal_result = None
        try:
            terminal_result = await asyncio.shield(_await_subagent_terminal(task_id, max_poll_count))
        except asyncio.CancelledError:
            pass

        # 报告子代理收集到的数据（即使我们超时了）
        final_result = terminal_result or get_background_task_result(task_id)
        if final_result is not None:
            _report_subagent_usage(runtime, final_result)
        if final_result is not None and _is_subagent_terminal(final_result):
            cleanup_background_task(task_id)
        else:
            # 子代理尚未终止，安排延迟清理
            _schedule_deferred_subagent_cleanup(task_id, trace_id, max_poll_count)
        _subagent_usage_cache.pop(tool_call_id, None)
        raise
    except Exception:
        _subagent_usage_cache.pop(tool_call_id, None)
        raise
