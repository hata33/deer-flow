"""线程级别的运行（Thread Runs）管理路由。

本模块实现了线程范围的运行生命周期管理，是 DeerFlow 运行系统的核心
路由。运行（Run）代表一次 AI 智能体的执行过程，可以流式或阻塞方式
返回结果。

核心端点：
- POST /{thread_id}/runs — 创建后台运行（立即返回）
- POST /{thread_id}/runs/stream — 创建运行并通过 SSE 流式返回事件
- POST /{thread_id}/runs/wait — 创建运行并阻塞等待完成
- GET /{thread_id}/runs — 列出线程的所有运行
- GET /{thread_id}/runs/{run_id} — 获取运行详情
- POST /{thread_id}/runs/{run_id}/cancel — 取消运行

SSE 兼容性：
- 流格式遵循 LangGraph Platform 协议，useStream React hook 可直接使用
- Content-Location 头包含运行资源 URL，SDK 用其提取运行元数据

取消策略：
- interrupt: 停止执行，保留当前检查点（可恢复）
- rollback: 停止执行，回滚到运行前的检查点状态

附加端点：
- 消息分页查询（线程级和运行级）
- 事件流查询（调试/审计）
- 线程令牌用量聚合统计

路由前缀：/api/threads
标签：runs
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.gateway.authz import require_permission
from app.gateway.deps import get_checkpointer, get_current_user, get_feedback_repo, get_run_event_store, get_run_manager, get_run_store, get_stream_bridge
from app.gateway.services import sse_consumer, start_run
from deerflow.runtime import RunRecord, RunStatus, serialize_channel_values

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/threads", tags=["runs"])


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class RunCreateRequest(BaseModel):
    """运行创建请求模型。

    包含 LangGraph Platform 运行 API 的完整参数集。

    Attributes:
        assistant_id: 使用的智能体/助手 ID。
        input: 图输入数据（如 {messages: [...]}）。
        command: LangGraph Command 对象。
        metadata: 运行元数据。
        config: RunnableConfig 覆盖配置。
        context: DeerFlow 上下文覆盖（model_name, thinking_enabled 等）。
        webhook: 完成回调 URL。
        checkpoint_id: 从指定检查点恢复。
        checkpoint: 完整检查点对象。
        interrupt_before: 在这些节点之前中断执行。
        interrupt_after: 在这些节点之后中断执行。
        stream_mode: 流模式（单个或多个）。
        stream_subgraphs: 是否包含子图事件。
        stream_resumable: SSE 可恢复模式。
        on_disconnect: SSE 断开时的行为（cancel 或 continue）。
        on_completion: 完成后是否删除临时线程。
        multitask_strategy: 并发策略（reject/rollback/interrupt/enqueue）。
        after_seconds: 延迟执行秒数。
        if_not_exists: 线程不存在时的策略（reject 或 create）。
        feedback_keys: LangSmith 反馈键。
    """

    assistant_id: str | None = Field(default=None, description="Agent / assistant to use")
    input: dict[str, Any] | None = Field(default=None, description="Graph input (e.g. {messages: [...]})")
    command: dict[str, Any] | None = Field(default=None, description="LangGraph Command")
    metadata: dict[str, Any] | None = Field(default=None, description="Run metadata")
    config: dict[str, Any] | None = Field(default=None, description="RunnableConfig overrides")
    context: dict[str, Any] | None = Field(default=None, description="DeerFlow context overrides (model_name, thinking_enabled, etc.)")
    webhook: str | None = Field(default=None, description="Completion callback URL")
    checkpoint_id: str | None = Field(default=None, description="Resume from checkpoint")
    checkpoint: dict[str, Any] | None = Field(default=None, description="Full checkpoint object")
    interrupt_before: list[str] | Literal["*"] | None = Field(default=None, description="Nodes to interrupt before")
    interrupt_after: list[str] | Literal["*"] | None = Field(default=None, description="Nodes to interrupt after")
    stream_mode: list[str] | str | None = Field(default=None, description="Stream mode(s)")
    stream_subgraphs: bool = Field(default=False, description="Include subgraph events")
    stream_resumable: bool | None = Field(default=None, description="SSE resumable mode")
    on_disconnect: Literal["cancel", "continue"] = Field(default="cancel", description="Behaviour on SSE disconnect")
    on_completion: Literal["delete", "keep"] = Field(default="keep", description="Delete temp thread on completion")
    multitask_strategy: Literal["reject", "rollback", "interrupt", "enqueue"] = Field(default="reject", description="Concurrency strategy")
    after_seconds: float | None = Field(default=None, description="Delayed execution")
    if_not_exists: Literal["reject", "create"] = Field(default="create", description="Thread creation policy")
    feedback_keys: list[str] | None = Field(default=None, description="LangSmith feedback keys")


class RunResponse(BaseModel):
    """运行响应模型。

    Attributes:
        run_id: 运行唯一标识符。
        thread_id: 所属线程 ID。
        assistant_id: 使用的智能体 ID。
        status: 运行状态。
        metadata: 运行元数据。
        kwargs: 额外参数。
        multitask_strategy: 并发策略。
        created_at: 创建时间。
        updated_at: 更新时间。
    """

    run_id: str
    thread_id: str
    assistant_id: str | None = None
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    multitask_strategy: str = "reject"
    created_at: str = ""
    updated_at: str = ""


class ThreadTokenUsageModelBreakdown(BaseModel):
    """按模型分组的令牌用量明细。

    Attributes:
        tokens: 令牌总数。
        runs: 运行次数。
    """

    tokens: int = 0
    runs: int = 0


class ThreadTokenUsageCallerBreakdown(BaseModel):
    """按调用者分组的令牌用量明细。

    Attributes:
        lead_agent: 主智能体消耗的令牌数。
        subagent: 子智能体消耗的令牌数。
        middleware: 中间件消耗的令牌数。
    """

    lead_agent: int = 0
    subagent: int = 0
    middleware: int = 0


class ThreadTokenUsageResponse(BaseModel):
    """线程令牌用量聚合响应。

    Attributes:
        thread_id: 线程 ID。
        total_tokens: 令牌总数。
        total_input_tokens: 输入令牌总数。
        total_output_tokens: 输出令牌总数。
        total_runs: 运行总数。
        by_model: 按模型分组的用量明细。
        by_caller: 按调用者分组的用量明细。
    """

    thread_id: str
    total_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_runs: int = 0
    by_model: dict[str, ThreadTokenUsageModelBreakdown] = Field(default_factory=dict)
    by_caller: ThreadTokenUsageCallerBreakdown = Field(default_factory=ThreadTokenUsageCallerBreakdown)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _cancel_conflict_detail(run_id: str, record: RunRecord) -> str:
    """构建取消冲突时的错误详情消息。

    Args:
        run_id: 运行 ID。
        record: 运行记录对象。

    Returns:
        描述冲突原因的错误消息字符串。
    """
    if record.status in (RunStatus.pending, RunStatus.running):
        return f"Run {run_id} is not active on this worker and cannot be cancelled"
    return f"Run {run_id} is not cancellable (status: {record.status.value})"


def _record_to_response(record: RunRecord) -> RunResponse:
    """将内部 RunRecord 对象转换为 API 响应模型。

    Args:
        record: 运行记录对象。

    Returns:
        转换后的 RunResponse。
    """
    return RunResponse(
        run_id=record.run_id,
        thread_id=record.thread_id,
        assistant_id=record.assistant_id,
        status=record.status.value,
        metadata=record.metadata,
        kwargs=record.kwargs,
        multitask_strategy=record.multitask_strategy,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


# ---------------------------------------------------------------------------
# 端点实现
# ---------------------------------------------------------------------------


@router.post("/{thread_id}/runs", response_model=RunResponse)
@require_permission("runs", "create", owner_check=True, require_existing=True)
async def create_run(thread_id: str, body: RunCreateRequest, request: Request) -> RunResponse:
    """创建后台运行（立即返回，不等待完成）。

    Args:
        thread_id: 线程 ID。
        body: 运行创建请求体。
        request: FastAPI 请求对象。

    Returns:
        RunResponse，包含运行 ID 和初始状态。
    """
    record = await start_run(body, thread_id, request)
    return _record_to_response(record)


@router.post("/{thread_id}/runs/stream")
@require_permission("runs", "create", owner_check=True, require_existing=True)
async def stream_run(thread_id: str, body: RunCreateRequest, request: Request) -> StreamingResponse:
    """创建运行并通过 SSE 流式返回事件。

    响应包含 Content-Location 头，指向运行的资源 URL。
    LangGraph Platform 使用此头传递运行元数据，SDK 通过贪婪正则
    从中提取运行 ID，因此必须指向标准运行资源路径。

    Args:
        thread_id: 线程 ID。
        body: 运行创建请求体。
        request: FastAPI 请求对象。

    Returns:
        SSE StreamingResponse，实时推送运行事件。
    """
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
            # LangGraph Platform 在此头中包含运行元数据。
            # SDK 使用贪婪正则从此路径提取运行 ID，因此必须指向标准路径。
            "Content-Location": f"/api/threads/{thread_id}/runs/{record.run_id}",
        },
    )


@router.post("/{thread_id}/runs/wait", response_model=dict)
@require_permission("runs", "create", owner_check=True, require_existing=True)
async def wait_run(thread_id: str, body: RunCreateRequest, request: Request) -> dict:
    """创建运行并阻塞等待完成后返回最终状态。

    运行完成后从检查点读取最终状态。若检查点获取失败，
    回退到返回运行状态和错误信息。

    Args:
        thread_id: 线程 ID。
        body: 运行创建请求体。
        request: FastAPI 请求对象。

    Returns:
        运行完成后的最终状态字典。
    """
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


@router.get("/{thread_id}/runs", response_model=list[RunResponse])
@require_permission("runs", "read", owner_check=True)
async def list_runs(thread_id: str, request: Request) -> list[RunResponse]:
    """列出指定线程的所有运行记录。

    Args:
        thread_id: 线程 ID。
        request: FastAPI 请求对象。

    Returns:
        运行记录列表。
    """
    run_mgr = get_run_manager(request)
    user_id = await get_current_user(request)
    records = await run_mgr.list_by_thread(thread_id, user_id=user_id)
    return [_record_to_response(r) for r in records]


@router.get("/{thread_id}/runs/{run_id}", response_model=RunResponse)
@require_permission("runs", "read", owner_check=True)
async def get_run(thread_id: str, run_id: str, request: Request) -> RunResponse:
    """获取指定运行的详细信息。

    Args:
        thread_id: 线程 ID。
        run_id: 运行 ID。
        request: FastAPI 请求对象。

    Returns:
        RunResponse，运行详细信息。

    Raises:
        HTTPException: 状态码 404，当运行不存在或不属于该线程时抛出。
    """
    run_mgr = get_run_manager(request)
    user_id = await get_current_user(request)
    record = await run_mgr.get(run_id, user_id=user_id)
    if record is None or record.thread_id != thread_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return _record_to_response(record)


@router.post("/{thread_id}/runs/{run_id}/cancel")
@require_permission("runs", "cancel", owner_check=True, require_existing=True)
async def cancel_run(
    thread_id: str,
    run_id: str,
    request: Request,
    wait: bool = Query(default=False, description="Block until run completes after cancel"),
    action: Literal["interrupt", "rollback"] = Query(default="interrupt", description="Cancel action"),
) -> Response:
    """取消正在运行或等待中的运行。

    取消策略：
    - action=interrupt: 停止执行，保留当前检查点（可恢复）
    - action=rollback: 停止执行，回滚到运行前的检查点状态
    - wait=true: 阻塞直到运行完全停止后返回 204
    - wait=false: 立即返回 202

    Args:
        thread_id: 线程 ID。
        run_id: 运行 ID。
        request: FastAPI 请求对象。
        wait: 是否等待运行完全停止。
        action: 取消策略。

    Returns:
        204（等待完成）或 202（立即返回）。

    Raises:
        HTTPException: 状态码 404（运行不存在）或 409（无法取消）。
    """
    run_mgr = get_run_manager(request)
    record = await run_mgr.get(run_id)
    if record is None or record.thread_id != thread_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    cancelled = await run_mgr.cancel(run_id, action=action)
    if not cancelled:
        raise HTTPException(status_code=409, detail=_cancel_conflict_detail(run_id, record))

    # 等待运行任务完全停止
    if wait and record.task is not None:
        try:
            await record.task
        except asyncio.CancelledError:
            pass
        return Response(status_code=204)

    return Response(status_code=202)


@router.get("/{thread_id}/runs/{run_id}/join")
@require_permission("runs", "read", owner_check=True)
async def join_run(thread_id: str, run_id: str, request: Request) -> StreamingResponse:
    """加入正在运行的 SSE 事件流。

    用于在页面刷新或新标签页打开时重新连接到正在进行的运行流。

    Args:
        thread_id: 线程 ID。
        run_id: 运行 ID。
        request: FastAPI 请求对象。

    Returns:
        SSE StreamingResponse。

    Raises:
        HTTPException: 状态码 404（运行不存在）或 409（运行不在本 Worker 上）。
    """
    run_mgr = get_run_manager(request)
    record = await run_mgr.get(run_id)
    if record is None or record.thread_id != thread_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    # 仅 store_only 的记录不在本 Worker 上，无法流式传输
    if record.store_only:
        raise HTTPException(status_code=409, detail=f"Run {run_id} is not active on this worker and cannot be streamed")

    bridge = get_stream_bridge(request)
    return StreamingResponse(
        sse_consumer(bridge, record, request, run_mgr),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.api_route("/{thread_id}/runs/{run_id}/stream", methods=["GET", "POST"], response_model=None)
@require_permission("runs", "read", owner_check=True)
async def stream_existing_run(
    thread_id: str,
    run_id: str,
    request: Request,
    action: Literal["interrupt", "rollback"] | None = Query(default=None, description="Cancel action"),
    wait: int = Query(default=0, description="Block until cancelled (1) or return immediately (0)"),
):
    """加入或取消后加入正在运行的 SSE 事件流。

    GET: 加入现有运行的 SSE 流（纯读取）。
    POST: 先取消运行（如 action 参数存在），再流式返回剩余缓冲事件。

    LangGraph SDK 的 joinStream 和 useStream 停止按钮均使用 POST
    请求到此端点。当 action=interrupt 或 action=rollback 时，
    先取消运行，然后流式返回剩余缓冲事件，使客户端观察到干净的关闭过程。

    Args:
        thread_id: 线程 ID。
        run_id: 运行 ID。
        request: FastAPI 请求对象。
        action: 取消策略（仅 POST 有效）。
        wait: 是否等待取消完成（1=等待, 0=立即）。

    Returns:
        SSE StreamingResponse 或 204 Response。

    Raises:
        HTTPException: 状态码 404（运行不存在）或 409（无法操作）。
    """
    run_mgr = get_run_manager(request)
    record = await run_mgr.get(run_id)
    if record is None or record.thread_id != thread_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if record.store_only and action is None:
        raise HTTPException(status_code=409, detail=f"Run {run_id} is not active on this worker and cannot be streamed")

    # 如果请求中包含 action 参数（停止按钮/中断流程），先取消运行
    if action is not None:
        cancelled = await run_mgr.cancel(run_id, action=action)
        if not cancelled:
            raise HTTPException(status_code=409, detail=_cancel_conflict_detail(run_id, record))
        if wait and record.task is not None:
            try:
                await record.task
            except (asyncio.CancelledError, Exception):
                pass
            return Response(status_code=204)

    bridge = get_stream_bridge(request)
    return StreamingResponse(
        sse_consumer(bridge, record, request, run_mgr),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# 消息 / 事件 / 令牌用量端点
# ---------------------------------------------------------------------------


@router.get("/{thread_id}/messages")
@require_permission("runs", "read", owner_check=True)
async def list_thread_messages(
    thread_id: str,
    request: Request,
    limit: int = Query(default=50, le=200),
    before_seq: int | None = Query(default=None),
    after_seq: int | None = Query(default=None),
) -> list[dict]:
    """返回线程的展示消息列表（跨所有运行），并附加反馈信息。

    查询线程内所有运行的消息，并在每个运行的最后一条 AI 消息上
    附加该用户的反馈数据。

    Args:
        thread_id: 线程 ID。
        request: FastAPI 请求对象。
        limit: 返回消息数量上限。
        before_seq: 向前翻页游标。
        after_seq: 向后翻页游标。

    Returns:
        消息列表，每条消息附带 feedback 字段。
    """
    event_store = get_run_event_store(request)
    messages = await event_store.list_messages(thread_id, limit=limit, before_seq=before_seq, after_seq=after_seq)

    # 为每个运行的最后一条 AI 消息附加反馈信息
    feedback_repo = get_feedback_repo(request)
    user_id = await get_current_user(request)
    feedback_map = await feedback_repo.list_by_thread_grouped(thread_id, user_id=user_id)

    # 找到每个运行的最后一条 ai_message 的索引
    last_ai_per_run: dict[str, int] = {}  # run_id -> 消息列表中的索引
    for i, msg in enumerate(messages):
        if msg.get("event_type") == "ai_message":
            last_ai_per_run[msg["run_id"]] = i

    # 附加 feedback 字段
    last_ai_indices = set(last_ai_per_run.values())
    for i, msg in enumerate(messages):
        if i in last_ai_indices:
            run_id = msg["run_id"]
            fb = feedback_map.get(run_id)
            msg["feedback"] = (
                {
                    "feedback_id": fb["feedback_id"],
                    "rating": fb["rating"],
                    "comment": fb.get("comment"),
                }
                if fb
                else None
            )
        else:
            msg["feedback"] = None

    return messages


@router.get("/{thread_id}/runs/{run_id}/messages")
@require_permission("runs", "read", owner_check=True)
async def list_run_messages(
    thread_id: str,
    run_id: str,
    request: Request,
    limit: int = Query(default=50, le=200, ge=1),
    before_seq: int | None = Query(default=None),
    after_seq: int | None = Query(default=None),
) -> dict:
    """按游标分页查询指定运行的消息列表。

    Args:
        thread_id: 线程 ID。
        run_id: 运行 ID。
        request: FastAPI 请求对象。
        limit: 每页消息数量。
        before_seq: 向前翻页游标。
        after_seq: 向后翻页游标。

    Returns:
        包含 data（消息列表）和 has_more（是否有更多）的字典。
    """
    event_store = get_run_event_store(request)
    # 多读一条用于判断是否还有更多数据
    rows = await event_store.list_messages_by_run(
        thread_id,
        run_id,
        limit=limit + 1,
        before_seq=before_seq,
        after_seq=after_seq,
    )
    has_more = len(rows) > limit
    data = rows[:limit] if has_more else rows
    return {"data": data, "has_more": has_more}


@router.get("/{thread_id}/runs/{run_id}/events")
@require_permission("runs", "read", owner_check=True)
async def list_run_events(
    thread_id: str,
    run_id: str,
    request: Request,
    event_types: str | None = Query(default=None),
    limit: int = Query(default=500, le=2000),
) -> list[dict]:
    """返回指定运行的完整事件流（调试/审计用）。

    可按事件类型过滤，支持逗号分隔的多种类型。

    Args:
        thread_id: 线程 ID。
        run_id: 运行 ID。
        request: FastAPI 请求对象。
        event_types: 事件类型过滤（逗号分隔）。
        limit: 返回事件数量上限。

    Returns:
        事件字典列表。
    """
    event_store = get_run_event_store(request)
    types = event_types.split(",") if event_types else None
    return await event_store.list_events(thread_id, run_id, event_types=types, limit=limit)


@router.get("/{thread_id}/token-usage", response_model=ThreadTokenUsageResponse)
@require_permission("threads", "read", owner_check=True)
async def thread_token_usage(thread_id: str, request: Request) -> ThreadTokenUsageResponse:
    """线程级别的令牌用量聚合统计。

    汇总线程内所有运行的令牌使用情况，按模型和调用者分组统计。

    Args:
        thread_id: 线程 ID。
        request: FastAPI 请求对象。

    Returns:
        ThreadTokenUsageResponse，包含各项令牌用量统计。
    """
    run_store = get_run_store(request)
    agg = await run_store.aggregate_tokens_by_thread(thread_id)
    return ThreadTokenUsageResponse(thread_id=thread_id, **agg)
