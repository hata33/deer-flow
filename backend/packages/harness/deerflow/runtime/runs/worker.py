"""后台 agent 执行模块。

在 ``asyncio.Task`` 中运行 agent 图，将事件发布到
:class:`StreamBridge`。

使用 ``graph.astream(stream_mode=[...])`` 为 ``values`` 模式提供正确的完整状态
快照，为 ``updates`` 提供正确的 ``{node: writes}``，并为 ``messages`` 模式
提供 ``(chunk, metadata)`` 元组。

注意: ``events`` 模式通过网关不受支持 —— 它需要
``graph.astream_events()``，它不能同时产生 ``values`` 快照。
JS 开源 LangGraph API 服务器通过内部检查点回调解决此问题，
这些回调在 Python 公共 API 中不公开。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal, cast

from langgraph.checkpoint.base import empty_checkpoint

if TYPE_CHECKING:
    from langchain_core.messages import HumanMessage

from deerflow.config.app_config import AppConfig
from deerflow.runtime.serialization import serialize
from deerflow.runtime.stream_bridge import StreamBridge
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.tracing import inject_langfuse_metadata

from .manager import RunManager, RunRecord
from .naming import resolve_root_run_name
from .schemas import RunStatus

logger = logging.getLogger(__name__)

# LangGraph 的 graph.astream() 的有效 stream_mode 值
_VALID_LG_MODES = {"values", "updates", "checkpoints", "tasks", "debug", "messages", "custom"}


def _build_runtime_context(
    thread_id: str,
    run_id: str,
    caller_context: Any | None,
    app_config: AppConfig | None = None,
) -> dict[str, Any]:
    """构建成为运行的 ``ToolRuntime.context`` 的字典。

    始终包括 ``thread_id`` 和 ``run_id``。来自调用者的
    ``config['context']`` 的额外键（例如 bootstrap 流的 ``agent_name`` —— issue #2677）
    被合并，但从不覆盖 ``thread_id``/``run_id``。解析的
    ``AppConfig`` 由 worker 添加，以便工具可以在不使用环境
    全局查找的情况下使用它。

    langgraph 1.1+ 通过存储在 ``config['configurable']['__pregel_runtime']`` 下的
    父运行时将其作为 ``runtime.context`` 公开 —— 参见
    ``langgraph.pregel.main``，其中调用了 ``parent_runtime.merge(...)``。
    """
    runtime_ctx: dict[str, Any] = {"thread_id": thread_id, "run_id": run_id}
    if isinstance(caller_context, dict):
        for key, value in caller_context.items():
            runtime_ctx.setdefault(key, value)
    if app_config is not None:
        runtime_ctx["app_config"] = app_config
    return runtime_ctx


@dataclass(frozen=True)
class RunContext:
    """单个 agent 运行的基础设施依赖项。

    将 checkpointer、store 和持久化相关的单例分组，以便
    ``run_agent``（以及任何未来的调用者）接收一个对象而不是
    不断增长的关键字参数列表。

    Attributes:
        checkpointer: 检查点实例
        store: 存储实例
        event_store: 事件存储实例
        run_events_config: 运行事件配置
        thread_store: 线程存储实例
        app_config: 应用配置
    """

    checkpointer: Any
    store: Any | None = field(default=None)
    event_store: Any | None = field(default=None)
    run_events_config: Any | None = field(default=None)
    thread_store: Any | None = field(default=None)
    app_config: AppConfig | None = field(default=None)


def _install_runtime_context(config: dict, runtime_context: dict[str, Any]) -> None:
    """将运行时上下文安装到配置中。

    Args:
        config: 配置字典
        runtime_context: 运行时上下文字典
    """
    existing_context = config.get("context")
    if isinstance(existing_context, dict):
        existing_context.setdefault("thread_id", runtime_context["thread_id"])
        existing_context.setdefault("run_id", runtime_context["run_id"])
        if "app_config" in runtime_context:
            existing_context["app_config"] = runtime_context["app_config"]
        return

    config["context"] = dict(runtime_context)


def _compute_agent_factory_supports_app_config(agent_factory: Any) -> bool:
    """计算 agent_factory 是否支持 app_config 参数。

    Args:
        agent_factory: agent 工厂函数

    Returns:
        是否支持 app_config 参数
    """
    try:
        return "app_config" in inspect.signature(agent_factory).parameters
    except (TypeError, ValueError):
        return False


@lru_cache(maxsize=128)
def _cached_agent_factory_supports_app_config(agent_factory: Any) -> bool:
    """缓存版本的 agent_factory app_config 支持检查。"""
    return _compute_agent_factory_supports_app_config(agent_factory)


def _agent_factory_supports_app_config(agent_factory: Any) -> bool:
    """检查 agent_factory 是否支持 app_config 参数。

    Args:
        agent_factory: agent 工厂函数

    Returns:
        是否支持 app_config 参数
    """
    try:
        return _cached_agent_factory_supports_app_config(agent_factory)
    except TypeError:
        # 某些可调用实例不可哈希；回退到直接检查
        return _compute_agent_factory_supports_app_config(agent_factory)


async def run_agent(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    *,
    ctx: RunContext,
    agent_factory: Any,
    graph_input: dict,
    config: dict,
    stream_modes: list[str] | None = None,
    stream_subgraphs: bool = False,
    interrupt_before: list[str] | Literal["*"] | None = None,
    interrupt_after: list[str] | Literal["*"] | None = None,
) -> None:
    """在后台执行 agent，将事件发布到 *bridge*。

    Args:
        bridge: 流桥接实例
        run_manager: 运行管理器
        record: 运行记录
        ctx: 运行上下文
        agent_factory: agent 工厂函数
        graph_input: 图输入
        config: 配置字典
        stream_modes: 流模式列表
        stream_subgraphs: 是否流式传输子图
        interrupt_before: 中断前的节点列表
        interrupt_after: 中断后的节点列表
    """

    # 从 RunContext 解包基础设施依赖项
    checkpointer = ctx.checkpointer
    store = ctx.store
    event_store = ctx.event_store
    run_events_config = ctx.run_events_config
    thread_store = ctx.thread_store

    run_id = record.run_id
    thread_id = record.thread_id
    requested_modes: set[str] = set(stream_modes or ["values"])
    pre_run_checkpoint_id: str | None = None
    pre_run_snapshot: dict[str, Any] | None = None
    snapshot_capture_failed = False
    llm_error_fallback_message: str | None = None

    # 跟踪是否请求了 "events" 但被跳过
    if "events" in requested_modes:
        logger.info(
            "Run %s: 'events' stream_mode not supported in gateway (requires astream_events + checkpoint callbacks). Skipping.",
            run_id,
        )

    try:
        # 初始化 RunJournal + 写入 human_message 事件
        # 这些在 try 块内，以便任何异常（例如写入事件的 DB 错误）
        # 通过发布"结束"事件到 SSE 桥接的 except/finally 路径流动 ——
        # 否则这里的失败将使流挂起而没有终止符
        if event_store is not None:
            from deerflow.runtime.journal import RunJournal

            journal = RunJournal(
                run_id=run_id,
                thread_id=thread_id,
                event_store=event_store,
                track_token_usage=getattr(run_events_config, "track_token_usage", True),
                progress_reporter=lambda snapshot: run_manager.update_run_progress(run_id, **snapshot),
            )

        # 1. 标记为运行中
        await run_manager.set_status(run_id, RunStatus.running)

        # 捕获运行前的最新检查点快照，以便回滚可以恢复它
        if checkpointer is not None:
            try:
                config_for_check = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
                ckpt_tuple = await checkpointer.aget_tuple(config_for_check)
                if ckpt_tuple is not None:
                    ckpt_config = getattr(ckpt_tuple, "config", {}).get("configurable", {})
                    pre_run_checkpoint_id = ckpt_config.get("checkpoint_id")
                    pre_run_snapshot = {
                        "checkpoint_ns": ckpt_config.get("checkpoint_ns", ""),
                        "checkpoint": copy.deepcopy(getattr(ckpt_tuple, "checkpoint", {})),
                        "metadata": copy.deepcopy(getattr(ckpt_tuple, "metadata", {})),
                        "pending_writes": copy.deepcopy(getattr(ckpt_tuple, "pending_writes", []) or []),
                    }
            except Exception:
                snapshot_capture_failed = True
                logger.warning("Could not capture pre-run checkpoint snapshot for run %s", run_id, exc_info=True)

        # 2. 发布元数据 —— useStream 需要 run_id 和 thread_id
        await bridge.publish(
            run_id,
            "metadata",
            {
                "run_id": run_id,
                "thread_id": thread_id,
            },
        )

        # 3. 构建 agent
        from langchain_core.runnables import RunnableConfig
        from langgraph.runtime import Runtime

        # 注入运行时上下文，以便 middlewares 和工具（通过 ToolRuntime.context）可以
        # 访问线程级数据。langgraph-cli 自动执行此操作；我们必须在此手动执行，
        # 因为我们通过 ``agent.astream(config=...)`` 驱动图，而不传递
        # 正式的 ``context=`` 参数
        runtime_ctx = _build_runtime_context(thread_id, run_id, config.get("context"), ctx.app_config)
        # Expose the run-scoped journal under a sentinel key so middleware can
        # write audit events (e.g. SafetyFinishReasonMiddleware recording
        # suppressed tool calls). Double-underscore prefix marks it as a
        # runtime-internal channel; user code must not depend on the key name.
        if journal is not None:
            runtime_ctx["__run_journal"] = journal
        _install_runtime_context(config, runtime_ctx)
        runtime = Runtime(context=cast(Any, runtime_ctx), store=store)
        config.setdefault("configurable", {})["__pregel_runtime"] = runtime

        # 将 RunJournal 注入为 LangChain 回调处理器
        # on_llm_end 捕获 token 使用量；on_chain_start/end 捕获生命周期
        if journal is not None:
            config.setdefault("callbacks", []).append(journal)

        # Inject Langfuse trace-attribute metadata so the langchain CallbackHandler
        # can lift session_id / user_id / trace_name / tags onto the root trace.
        # Shared helper with ``DeerFlowClient.stream`` so both entry points stay
        # in sync; caller-provided metadata wins via setdefault inside the helper.
        inject_langfuse_metadata(
            config,
            thread_id=thread_id,
            user_id=get_effective_user_id(),
            assistant_id=record.assistant_id,
            model_name=record.model_name,
            environment=os.environ.get("DEER_FLOW_ENV") or os.environ.get("ENVIRONMENT"),
        )

        # Resolve after runtime context installation so context/configurable reflect
        # the agent name that this run will actually execute.
        config.setdefault("run_name", resolve_root_run_name(config, record.assistant_id))
        runnable_config = RunnableConfig(**config)
        if ctx.app_config is not None and _agent_factory_supports_app_config(agent_factory):
            agent = agent_factory(config=runnable_config, app_config=ctx.app_config)
        else:
            agent = agent_factory(config=runnable_config)

        # 从 agent 的元数据中捕获有效的（解析的）模型名称
        # agent.py 中的 _resolve_model_name 可能返回默认模型（如果请求的名称不在允许列表中）——
        # 此更新确保持久的 model_name 反映实际使用的模型
        if record.model_name is not None:
            resolved = getattr(agent, "metadata", {}) or {}
            if isinstance(resolved, dict):
                effective = resolved.get("model_name")
                if effective and effective != record.model_name:
                    await run_manager.update_model_name(record.run_id, effective)

        # 4. 附加 checkpointer 和 store
        if checkpointer is not None:
            agent.checkpointer = checkpointer
        if store is not None:
            agent.store = store

        # 5. 设置中断节点
        if interrupt_before:
            agent.interrupt_before_nodes = interrupt_before
        if interrupt_after:
            agent.interrupt_after_nodes = interrupt_after

        # 6. 构建 LangGraph stream_mode 列表
        #    "events" 不是有效的 astream 模式 —— 跳过它
        #    "messages-tuple" 映射到 LangGraph 的 "messages" 模式
        lg_modes: list[str] = []
        for m in requested_modes:
            if m == "messages-tuple":
                lg_modes.append("messages")
            elif m == "events":
                # 跳过 —— 参见上面的日志
                continue
            elif m in _VALID_LG_MODES:
                lg_modes.append(m)
        if not lg_modes:
            lg_modes = ["values"]

        # 去重同时保持顺序
        seen: set[str] = set()
        deduped: list[str] = []
        for m in lg_modes:
            if m not in seen:
                seen.add(m)
                deduped.append(m)
        lg_modes = deduped

        logger.info("Run %s: streaming with modes %s (requested: %s)", run_id, lg_modes, requested_modes)

        # 7. 使用 graph.astream 流式传输
        if len(lg_modes) == 1 and not stream_subgraphs:
            # 单模式，无子图：astream 产生原始块
            single_mode = lg_modes[0]
            async for chunk in agent.astream(graph_input, config=runnable_config, stream_mode=single_mode):
                if record.abort_event.is_set():
                    logger.info("Run %s abort requested — stopping", run_id)
                    break
                llm_error_fallback_message = llm_error_fallback_message or _extract_llm_error_fallback_message(chunk)
                sse_event = _lg_mode_to_sse_event(single_mode)
                await bridge.publish(run_id, sse_event, serialize(chunk, mode=single_mode))
        else:
            # 多模式或子图：astream 产生元组
            async for item in agent.astream(
                graph_input,
                config=runnable_config,
                stream_mode=lg_modes,
                subgraphs=stream_subgraphs,
            ):
                if record.abort_event.is_set():
                    logger.info("Run %s abort requested — stopping", run_id)
                    break

                mode, chunk = _unpack_stream_item(item, lg_modes, stream_subgraphs)
                if mode is None:
                    continue

                llm_error_fallback_message = llm_error_fallback_message or _extract_llm_error_fallback_message(chunk)
                sse_event = _lg_mode_to_sse_event(mode)
                await bridge.publish(run_id, sse_event, serialize(chunk, mode=mode))

        # 8. 最终状态
        if record.abort_event.is_set():
            action = record.abort_action
            if action == "rollback":
                await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
                try:
                    await _rollback_to_pre_run_checkpoint(
                        checkpointer=checkpointer,
                        thread_id=thread_id,
                        run_id=run_id,
                        pre_run_checkpoint_id=pre_run_checkpoint_id,
                        pre_run_snapshot=pre_run_snapshot,
                        snapshot_capture_failed=snapshot_capture_failed,
                    )
                    logger.info("Run %s rolled back to pre-run checkpoint %s", run_id, pre_run_checkpoint_id)
                except Exception:
                    logger.warning("Failed to rollback checkpoint for run %s", run_id, exc_info=True)
            else:
                await run_manager.set_status(run_id, RunStatus.interrupted)
        elif llm_error_fallback_message or (journal is not None and journal.had_llm_error_fallback):
            error_msg = llm_error_fallback_message
            if error_msg is None and journal is not None:
                error_msg = journal.llm_error_fallback_message
            error_msg = error_msg or "LLM provider failed after retries"
            await run_manager.set_status(run_id, RunStatus.error, error=error_msg)
        else:
            await run_manager.set_status(run_id, RunStatus.success)

    except asyncio.CancelledError:
        action = record.abort_action
        if action == "rollback":
            await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
            try:
                await _rollback_to_pre_run_checkpoint(
                    checkpointer=checkpointer,
                    thread_id=thread_id,
                    run_id=run_id,
                    pre_run_checkpoint_id=pre_run_checkpoint_id,
                    pre_run_snapshot=pre_run_snapshot,
                    snapshot_capture_failed=snapshot_capture_failed,
                )
                logger.info("Run %s was cancelled and rolled back", run_id)
            except Exception:
                logger.warning("Run %s cancellation rollback failed", run_id, exc_info=True)
        else:
            await run_manager.set_status(run_id, RunStatus.interrupted)
            logger.info("Run %s was cancelled", run_id)

    except Exception as exc:
        error_msg = f"{exc}"
        logger.exception("Run %s failed: %s", run_id, error_msg)
        await run_manager.set_status(run_id, RunStatus.error, error=error_msg)
        await bridge.publish(
            run_id,
            "error",
            {
                "message": error_msg,
                "name": type(exc).__name__,
            },
        )

    finally:
        # 刷新任何缓冲的日志事件并持久化完成数据
        if journal is not None:
            try:
                await journal.flush()
            except Exception:
                logger.warning("Failed to flush journal for run %s", run_id, exc_info=True)

            try:
                # 将 token 使用量 + 便捷字段持久化到 RunStore
                completion = journal.get_completion_data()
                await run_manager.update_run_completion(run_id, status=record.status.value, **completion)
            except Exception:
                logger.warning("Failed to persist run completion for %s (non-fatal)", run_id, exc_info=True)

        # 从检查点同步标题到 threads_meta.display_name
        if checkpointer is not None and thread_store is not None:
            try:
                ckpt_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
                ckpt_tuple = await checkpointer.aget_tuple(ckpt_config)
                if ckpt_tuple is not None:
                    ckpt = getattr(ckpt_tuple, "checkpoint", {}) or {}
                    title = ckpt.get("channel_values", {}).get("title")
                    if title:
                        await thread_store.update_display_name(thread_id, title)
            except Exception:
                logger.debug("Failed to sync title for thread %s (non-fatal)", thread_id)

        # 根据运行结果更新 threads_meta 状态
        if thread_store is not None:
            try:
                final_status = "idle" if record.status == RunStatus.success else record.status.value
                await thread_store.update_status(thread_id, final_status)
            except Exception:
                logger.debug("Failed to update thread_meta status for %s (non-fatal)", thread_id)

        await bridge.publish_end(run_id)
        asyncio.create_task(bridge.cleanup(run_id, delay=60))


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


async def _call_checkpointer_method(checkpointer: Any, async_name: str, sync_name: str, *args: Any, **kwargs: Any) -> Any:
    """调用 checkpointer 方法，支持异步和同步变体。

    Args:
        checkpointer: 检查点实例
        async_name: 异步方法名称
        sync_name: 同步方法名称
        *args: 位置参数
        **kwargs: 关键字参数

    Returns:
        方法调用结果

    Raises:
        AttributeError: 如果方法不存在
    """
    method = getattr(checkpointer, async_name, None) or getattr(checkpointer, sync_name, None)
    if method is None:
        raise AttributeError(f"Missing checkpointer method: {async_name}/{sync_name}")
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _rollback_to_pre_run_checkpoint(
    *,
    checkpointer: Any,
    thread_id: str,
    run_id: str,
    pre_run_checkpoint_id: str | None,
    pre_run_snapshot: dict[str, Any] | None,
    snapshot_capture_failed: bool,
) -> None:
    """将线程状态恢复到运行开始前捕获的检查点快照。

    Args:
        checkpointer: 检查点实例
        thread_id: 线程 ID
        run_id: 运行 ID
        pre_run_checkpoint_id: 运行前的检查点 ID
        pre_run_snapshot: 运行前的快照
        snapshot_capture_failed: 快照捕获是否失败
    """
    if checkpointer is None:
        logger.info("Run %s rollback requested but no checkpointer is configured", run_id)
        return

    if snapshot_capture_failed:
        logger.warning("Run %s rollback skipped: pre-run checkpoint snapshot capture failed", run_id)
        return

    if pre_run_snapshot is None:
        await _call_checkpointer_method(checkpointer, "adelete_thread", "delete_thread", thread_id)
        logger.info("Run %s rollback reset thread %s to empty state", run_id, thread_id)
        return

    checkpoint_to_restore = None
    metadata_to_restore: dict[str, Any] = {}
    checkpoint_ns = ""
    checkpoint = pre_run_snapshot.get("checkpoint")
    if not isinstance(checkpoint, dict):
        logger.warning("Run %s rollback skipped: invalid pre-run checkpoint snapshot", run_id)
        return
    checkpoint_to_restore = checkpoint
    if checkpoint_to_restore.get("id") is None and pre_run_checkpoint_id is not None:
        checkpoint_to_restore = {**checkpoint_to_restore, "id": pre_run_checkpoint_id}
    if checkpoint_to_restore.get("id") is None:
        logger.warning("Run %s rollback skipped: pre-run checkpoint has no checkpoint id", run_id)
        return
    restore_marker = _new_checkpoint_marker()
    checkpoint_to_restore = {
        **checkpoint_to_restore,
        "id": restore_marker["id"],
        "ts": restore_marker["ts"],
    }
    metadata = pre_run_snapshot.get("metadata", {})
    metadata_to_restore = metadata if isinstance(metadata, dict) else {}
    raw_checkpoint_ns = pre_run_snapshot.get("checkpoint_ns")
    checkpoint_ns = raw_checkpoint_ns if isinstance(raw_checkpoint_ns, str) else ""

    channel_versions = checkpoint_to_restore.get("channel_versions")
    new_versions = dict(channel_versions) if isinstance(channel_versions, dict) else {}

    restore_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}}
    restored_config = await _call_checkpointer_method(
        checkpointer,
        "aput",
        "put",
        restore_config,
        checkpoint_to_restore,
        metadata_to_restore if isinstance(metadata_to_restore, dict) else {},
        new_versions,
    )
    if not isinstance(restored_config, dict):
        raise RuntimeError(f"Run {run_id} rollback restore returned invalid config: expected dict")
    restored_configurable = restored_config.get("configurable", {})
    if not isinstance(restored_configurable, dict):
        raise RuntimeError(f"Run {run_id} rollback restore returned invalid config payload")
    restored_checkpoint_id = restored_configurable.get("checkpoint_id")
    if not restored_checkpoint_id:
        raise RuntimeError(f"Run {run_id} rollback restore did not return checkpoint_id")

    pending_writes = pre_run_snapshot.get("pending_writes", [])
    if not pending_writes:
        return

    writes_by_task: dict[str, list[tuple[str, Any]]] = {}
    for item in pending_writes:
        if not isinstance(item, (tuple, list)) or len(item) != 3:
            raise RuntimeError(f"Run {run_id} rollback failed: pending_write is not a 3-tuple: {item!r}")
        task_id, channel, value = item
        if not isinstance(channel, str):
            raise RuntimeError(f"Run {run_id} rollback failed: pending_write has non-string channel: task_id={task_id!r}, channel={channel!r}")
        writes_by_task.setdefault(str(task_id), []).append((channel, value))

    for task_id, writes in writes_by_task.items():
        await _call_checkpointer_method(
            checkpointer,
            "aput_writes",
            "put_writes",
            restored_config,
            writes,
            task_id=task_id,
        )


def _new_checkpoint_marker() -> dict[str, str]:
    """创建新的检查点标记。

    Returns:
        包含 id 和 ts 的字典
    """
    marker = empty_checkpoint()
    return {"id": marker["id"], "ts": marker["ts"]}


def _lg_mode_to_sse_event(mode: str) -> str:
    """将 LangGraph 内部 stream_mode 名称映射到 SSE 事件名称。

    LangGraph 的 ``astream(stream_mode="messages")`` 产生消息
    元组。SSE 协议在客户端显式请求时将其称为 ``messages-tuple``，
    但 LangGraph Platform 使用的默认 SSE 事件名称简单地是 ``"messages"``。

    Args:
        mode: LangGraph 流模式

    Returns:
        SSE 事件名称
    """
    # 所有 LG 模式 1:1 映射到 SSE 事件名称 —— "messages" 保持 "messages"
    return mode


def _error_fallback_message_from_metadata(metadata: dict[str, Any], content: Any) -> str:
    detail = metadata.get("error_detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    reason = metadata.get("error_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    if isinstance(content, str) and content.strip():
        return content.strip()[:2000]
    return "LLM provider failed after retries"


def _try_extract_from_message(obj: Any) -> str | None:
    """Try to extract fallback marker from a single message object or dict."""
    additional_kwargs = getattr(obj, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict) and additional_kwargs.get("deerflow_error_fallback"):
        return _error_fallback_message_from_metadata(additional_kwargs, getattr(obj, "content", None))

    if isinstance(obj, dict):
        nested_kwargs = obj.get("additional_kwargs")
        if isinstance(nested_kwargs, dict) and nested_kwargs.get("deerflow_error_fallback"):
            return _error_fallback_message_from_metadata(nested_kwargs, obj.get("content"))
    return None


def _extract_llm_error_fallback_message(value: Any) -> str | None:
    """Find LLM fallback markers in streamed LangGraph chunks.

    Error fallback messages returned by model-call middleware are not guaranteed
    to pass through LLM end callbacks, but they do appear in graph state chunks.
    """
    # Fast path: large state chunks produced by stream_mode="values" have a
    # top-level "messages" list. Scanning only that list avoids expensive deep
    # recursion into large state dicts.
    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, (list, tuple)):
            for msg in messages:
                result = _try_extract_from_message(msg)
                if result is not None:
                    return result
            # Fallback marker is attached to an AI message in the messages
            # channel; it will never appear elsewhere in a values chunk.
            return None
        # No top-level "messages" — this is likely an "updates" chunk (small
        # dict keyed by node name). Fall through to deep walk, which is cheap
        # for these payloads.

    # Deep walk for updates / messages / tuple / list modes. Payloads are
    # small, so full recursion is acceptable here.
    seen: set[int] = set()

    def walk(obj: Any) -> str | None:
        oid = id(obj)
        if oid in seen:
            return None
        seen.add(oid)

        result = _try_extract_from_message(obj)
        if result is not None:
            return result

        if isinstance(obj, dict):
            for item in obj.values():
                result = walk(item)
                if result is not None:
                    return result
            return None

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                result = walk(item)
                if result is not None:
                    return result
        return None

    return walk(value)


def _extract_human_message(graph_input: dict) -> HumanMessage | None:
    """从 graph_input 中提取或构造 HumanMessage 用于事件记录。

    返回 LangChain HumanMessage，以便调用者可以使用 .model_dump() 获取
    检查点对齐的序列化格式。

    Args:
        graph_input: 图输入字典

    Returns:
        HumanMessage 对象或 None
    """
    from langchain_core.messages import HumanMessage

    messages = graph_input.get("messages")
    if not messages:
        return None
    last = messages[-1] if isinstance(messages, list) else messages
    if isinstance(last, HumanMessage):
        return last
    if isinstance(last, str):
        return HumanMessage(content=last) if last else None
    if hasattr(last, "content"):
        content = last.content
        return HumanMessage(content=content)
    if isinstance(last, dict):
        content = last.get("content", "")
        return HumanMessage(content=content) if content else None
    return None


def _unpack_stream_item(
    item: Any,
    lg_modes: list[str],
    stream_subgraphs: bool,
) -> tuple[str | None, Any]:
    """将多模式或子图流项解包为 (mode, chunk)。

    如果项目无法解析，返回 ``(None, None)``。

    Args:
        item: 流项目
        lg_modes: LangGraph 模式列表
        stream_subgraphs: 是否流式传输子图

    Returns:
        (模式, 块) 元组
    """
    if stream_subgraphs:
        if isinstance(item, tuple) and len(item) == 3:
            _ns, mode, chunk = item
            return str(mode), chunk
        if isinstance(item, tuple) and len(item) == 2:
            mode, chunk = item
            return str(mode), chunk
        return None, None

    if isinstance(item, tuple) and len(item) == 2:
        mode, chunk = item
        return str(mode), chunk

    # 回退：来自第一个模式的单元素输出
    return lg_modes[0] if lg_modes else None, item
