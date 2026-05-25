"""无状态运行（Stateless Runs）端点路由。

本模块提供不依赖预存线程的运行创建接口。当请求中未提供 thread_id 时，
自动创建一个临时线程；当提供 thread_id 时，复用已有线程以保持对话历史。

核心端点：
- POST /stream — 创建运行并通过 SSE 流式返回事件
- POST /wait — 创建运行并阻塞等待完成后返回最终状态

附加端点：
- GET /{run_id}/messages — 按游标分页查询运行消息
- GET /{run_id}/feedback — 查询运行的反馈记录

SSE 流格式遵循 LangGraph Platform 协议，确保前端 useStream hook
无需修改即可对接。

路由前缀：/api/runs
标签：runs
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.gateway.authz import require_permission
from app.gateway.deps import get_checkpointer, get_feedback_repo, get_run_event_store, get_run_manager, get_run_store, get_stream_bridge
from app.gateway.routers.thread_runs import RunCreateRequest
from app.gateway.services import sse_consumer, start_run
from deerflow.runtime import serialize_channel_values

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/runs", tags=["runs"])


def _resolve_thread_id(body: RunCreateRequest) -> str:
    """从请求体中提取 thread_id，若不存在则生成新的 UUID。

    thread_id 位于 config.configurable.thread_id 路径中。

    Args:
        body: 运行创建请求体。

    Returns:
        已有或新生成的线程 ID。
    """
    thread_id = (body.config or {}).get("configurable", {}).get("thread_id")
    if thread_id:
        return str(thread_id)
    return str(uuid.uuid4())


@router.post("/stream")
async def stateless_stream(body: RunCreateRequest, request: Request) -> StreamingResponse:
    """创建无状态运行并通过 SSE 流式返回事件。

    若 config.configurable.thread_id 已提供，运行在已有线程上创建
    以保持对话历史连续性；否则自动创建新的临时线程。

    Args:
        body: 运行创建请求体。
        request: FastAPI 请求对象。

    Returns:
        SSE StreamingResponse，包含运行事件的实时流。
    """
    thread_id = _resolve_thread_id(body)
    bridge = get_stream_bridge(request)
    run_mgr = get_run_manager(request)
    record = await start_run(body, thread_id, request)

    return StreamingResponse(
        sse_consumer(bridge, record, request, run_mgr),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 禁用 Nginx 缓冲，确保 SSE 事件即时推送
            "X-Accel-Buffering": "no",
            "Content-Location": f"/api/threads/{thread_id}/runs/{record.run_id}",
        },
    )


@router.post("/wait", response_model=dict)
async def stateless_wait(body: RunCreateRequest, request: Request) -> dict:
    """创建无状态运行并阻塞等待完成。

    运行完成后从检查点中读取最终状态并返回。
    若获取检查点失败，回退到返回运行状态和错误信息。

    Args:
        body: 运行创建请求体。
        request: FastAPI 请求对象。

    Returns:
        运行完成后的最终状态字典。
    """
    thread_id = _resolve_thread_id(body)
    record = await start_run(body, thread_id, request)

    # 等待运行任务完成
    if record.task is not None:
        try:
            await record.task
        except asyncio.CancelledError:
            pass

    # 从检查点获取最终状态
    checkpointer = get_checkpointer(request)
    config = {"configurable": {"thread_id": thread_id}}
    try:
        checkpoint_tuple = await checkpointer.aget_tuple(config)
        if checkpoint_tuple is not None:
            checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}
            channel_values = checkpoint.get("channel_values", {})
            return serialize_channel_values(channel_values)
    except Exception:
        logger.exception("Failed to fetch final state for run %s", record.run_id)

    # 检查点获取失败时的降级响应
    return {"status": record.status.value, "error": record.error}


# ---------------------------------------------------------------------------
# 运行级别的只读端点
# ---------------------------------------------------------------------------


async def _resolve_run(run_id: str, request: Request) -> dict:
    """按 run_id 获取运行记录并校验用户归属。未找到时抛出 404。

    Args:
        run_id: 运行 ID。
        request: FastAPI 请求对象。

    Returns:
        运行记录字典。

    Raises:
        HTTPException: 状态码 404，当运行不存在时抛出。
    """
    run_store = get_run_store(request)
    # user_id=AUTO 通过上下文变量自动过滤当前用户的运行记录
    record = await run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return record


@router.get("/{run_id}/messages")
@require_permission("runs", "read")
async def run_messages(
    run_id: str,
    request: Request,
    limit: int = Query(default=50, le=200, ge=1),
    before_seq: int | None = Query(default=None),
    after_seq: int | None = Query(default=None),
) -> dict:
    """按游标分页查询运行的消息列表。

    分页模式：
    - after_seq: 返回 seq > after_seq 的消息（正向翻页）
    - before_seq: 返回 seq < before_seq 的消息（反向翻页）
    - 两者均不提供: 返回最新消息

    Args:
        run_id: 运行 ID。
        request: FastAPI 请求对象。
        limit: 每页消息数量（1-200，默认 50）。
        before_seq: 向前翻页游标。
        after_seq: 向后翻页游标。

    Returns:
        包含 data（消息列表）和 has_more（是否有更多）的字典。
    """
    run = await _resolve_run(run_id, request)
    event_store = get_run_event_store(request)
    # 多读一条用于判断是否还有更多数据
    rows = await event_store.list_messages_by_run(
        run["thread_id"],
        run_id,
        limit=limit + 1,
        before_seq=before_seq,
        after_seq=after_seq,
    )
    has_more = len(rows) > limit
    data = rows[:limit] if has_more else rows
    return {"data": data, "has_more": has_more}


@router.get("/{run_id}/feedback")
@require_permission("runs", "read")
async def run_feedback(run_id: str, request: Request) -> list[dict]:
    """查询指定运行的所有反馈记录。

    Args:
        run_id: 运行 ID。
        request: FastAPI 请求对象。

    Returns:
        反馈记录列表。
    """
    run = await _resolve_run(run_id, request)
    feedback_repo = get_feedback_repo(request)
    return await feedback_repo.list_by_run(run["thread_id"], run_id)
