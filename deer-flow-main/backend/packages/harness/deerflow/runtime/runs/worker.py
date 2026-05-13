"""后台 Agent 执行器。

在 asyncio.Task 中运行 Agent 图，将流式事件发布到 StreamBridge。
支持多流模式（values/updates/messages）和中断/回滚取消。
注意：events 模式不支持（需要 astream_events + 检查点回调，Python API 未暴露）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from deerflow.runtime.serialization import serialize
from deerflow.runtime.stream_bridge import StreamBridge

from .manager import RunManager, RunRecord
from .schemas import RunStatus

logger = logging.getLogger(__name__)

# LangGraph graph.astream() 支持的 stream_mode 值
_VALID_LG_MODES = {"values", "updates", "checkpoints", "tasks", "debug", "messages", "custom"}


async def run_agent(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    *,
    checkpointer: Any,
    store: Any | None = None,
    agent_factory: Any,
    graph_input: dict,
    config: dict,
    stream_modes: list[str] | None = None,
    stream_subgraphs: bool = False,
    interrupt_before: list[str] | Literal["*"] | None = None,
    interrupt_after: list[str] | Literal["*"] | None = None,
) -> None:
    """在后台执行 Agent，将事件发布到 bridge。"""

    run_id = record.run_id
    thread_id = record.thread_id
    requested_modes: set[str] = set(stream_modes or ["values"])

    # events 模式不支持，跳过并记录日志
    if "events" in requested_modes:
        logger.info(
            "Run %s: 'events' stream_mode not supported in gateway (requires astream_events + checkpoint callbacks). Skipping.",
            run_id,
        )

    try:
        # 1. 标记为运行中
        await run_manager.set_status(run_id, RunStatus.running)

        # 记录运行前检查点 ID（用于 rollback 支持）
        pre_run_checkpoint_id = None
        try:
            config_for_check = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
            ckpt_tuple = await checkpointer.aget_tuple(config_for_check)
            if ckpt_tuple is not None:
                pre_run_checkpoint_id = getattr(ckpt_tuple, "config", {}).get("configurable", {}).get("checkpoint_id")
        except Exception:
            logger.debug("Could not get pre-run checkpoint_id for run %s", run_id)

        # 2. 发布元数据（useStream 需要 run_id 和 thread_id）
        await bridge.publish(
            run_id,
            "metadata",
            {
                "run_id": run_id,
                "thread_id": thread_id,
            },
        )

        # 3. 构建 Agent
        from langchain_core.runnables import RunnableConfig
        from langgraph.runtime import Runtime

        # 注入运行时上下文（langgraph-cli 自动注入，嵌入式需手动）
        runtime = Runtime(context={"thread_id": thread_id}, store=store)
        config.setdefault("configurable", {})["__pregel_runtime"] = runtime

        runnable_config = RunnableConfig(**config)
        agent = agent_factory(config=runnable_config)

        # 4. 附加检查点和存储
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
        lg_modes: list[str] = []
        for m in requested_modes:
            if m == "messages-tuple":
                lg_modes.append("messages")  # 映射到 LangGraph 的 messages 模式
            elif m == "events":
                continue  # 跳过
            elif m in _VALID_LG_MODES:
                lg_modes.append(m)
        if not lg_modes:
            lg_modes = ["values"]

        # 去重并保持顺序
        seen: set[str] = set()
        deduped: list[str] = []
        for m in lg_modes:
            if m not in seen:
                seen.add(m)
                deduped.append(m)
        lg_modes = deduped

        logger.info("Run %s: streaming with modes %s (requested: %s)", run_id, lg_modes, requested_modes)

        # 7. 流式执行
        if len(lg_modes) == 1 and not stream_subgraphs:
            # 单模式、无子图：astream 直接产生原始 chunk
            single_mode = lg_modes[0]
            async for chunk in agent.astream(graph_input, config=runnable_config, stream_mode=single_mode):
                if record.abort_event.is_set():
                    logger.info("Run %s abort requested — stopping", run_id)
                    break
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

                sse_event = _lg_mode_to_sse_event(mode)
                await bridge.publish(run_id, sse_event, serialize(chunk, mode=mode))

        # 8. 最终状态
        if record.abort_event.is_set():
            action = record.abort_action
            if action == "rollback":
                await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
                # TODO(Phase 2): 实现完整的检查点回滚
                try:
                    if checkpointer is not None and pre_run_checkpoint_id is not None:
                        pass
                    logger.info("Run %s rolled back", run_id)
                except Exception:
                    logger.warning("Failed to rollback checkpoint for run %s", run_id)
            else:
                await run_manager.set_status(run_id, RunStatus.interrupted)
        else:
            await run_manager.set_status(run_id, RunStatus.success)

    except asyncio.CancelledError:
        action = record.abort_action
        if action == "rollback":
            await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
            logger.info("Run %s was cancelled (rollback)", run_id)
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
        await bridge.publish_end(run_id)
        asyncio.create_task(bridge.cleanup(run_id, delay=60))


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _lg_mode_to_sse_event(mode: str) -> str:
    """将 LangGraph 内部 stream_mode 名称映射为 SSE 事件名称。

    LangGraph 的 messages 模式产生消息元组，SSE 协议中称为 messages。
    """
    return mode


def _unpack_stream_item(
    item: Any,
    lg_modes: list[str],
    stream_subgraphs: bool,
) -> tuple[str | None, Any]:
    """解包多模式或子图流项目为 (mode, chunk)。"""
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

    # 兜底：单元素输出使用第一个模式
    return lg_modes[0] if lg_modes else None, item
