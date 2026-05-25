"""用户反馈（Feedback）的 CRUD 端点路由。

本模块实现了对 AI 运行（Run）的用户反馈管理，支持以下操作：

核心功能：
- 创建反馈（thumbs-up / thumbs-down），可关联到特定消息
- 幂等更新（upsert）——同一用户对同一运行的反馈会被覆盖
- 删除反馈——支持按运行删除和按反馈 ID 精确删除
- 列出运行的所有反馈
- 聚合统计（正面/负面计数）

数据模型：
- 反评分为 +1（正面）或 -1（负面）
- 可附加文字评论（comment）
- 可指定消息级别（message_id）的反馈

权限控制：
- 所有写操作需要 threads:write 权限
- 所有读操作需要 threads:read 权限
- 删除操作需要 threads:delete 权限
- 所有端点启用线程所有者校验

路由前缀：/api/threads
标签：feedback
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.gateway.authz import require_permission
from app.gateway.deps import get_current_user, get_feedback_repo, get_run_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/threads", tags=["feedback"])


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class FeedbackCreateRequest(BaseModel):
    """创建反馈请求模型。

    Attributes:
        rating: 反评评分，+1（正面）或 -1（负面）。
        comment: 可选的文字反馈。
        message_id: 可选的消息 ID，将反馈关联到特定消息。
    """

    rating: int = Field(..., description="Feedback rating: +1 (positive) or -1 (negative)")
    comment: str | None = Field(default=None, description="Optional text feedback")
    message_id: str | None = Field(default=None, description="Optional: scope feedback to a specific message")


class FeedbackUpsertRequest(BaseModel):
    """幂等更新反馈请求模型。

    Attributes:
        rating: 反评评分，+1（正面）或 -1（负面）。
        comment: 可选的文字反馈。
    """

    rating: int = Field(..., description="Feedback rating: +1 (positive) or -1 (negative)")
    comment: str | None = Field(default=None, description="Optional text feedback")


class FeedbackResponse(BaseModel):
    """反馈响应模型。

    Attributes:
        feedback_id: 反馈唯一标识符。
        run_id: 关联的运行 ID。
        thread_id: 关联的线程 ID。
        user_id: 提交反馈的用户 ID。
        message_id: 关联的消息 ID（可选）。
        rating: 反评评分。
        comment: 文字反馈。
        created_at: 创建时间。
    """

    feedback_id: str
    run_id: str
    thread_id: str
    user_id: str | None = None
    message_id: str | None = None
    rating: int
    comment: str | None = None
    created_at: str = ""


class FeedbackStatsResponse(BaseModel):
    """反馈统计响应模型。

    Attributes:
        run_id: 运行 ID。
        total: 总反馈数。
        positive: 正面反馈数。
        negative: 负面反馈数。
    """

    run_id: str
    total: int = 0
    positive: int = 0
    negative: int = 0


# ---------------------------------------------------------------------------
# 端点实现
# ---------------------------------------------------------------------------


@router.put("/{thread_id}/runs/{run_id}/feedback", response_model=FeedbackResponse)
@require_permission("threads", "write", owner_check=True, require_existing=True)
async def upsert_feedback(
    thread_id: str,
    run_id: str,
    body: FeedbackUpsertRequest,
    request: Request,
) -> dict[str, Any]:
    """创建或更新运行反馈（幂等操作）。

    同一用户对同一运行的反馈会被覆盖更新。

    Args:
        thread_id: 线程 ID。
        run_id: 运行 ID。
        body: 反馈请求体。
        request: FastAPI 请求对象。

    Returns:
        创建或更新后的反馈记录。

    Raises:
        HTTPException: 状态码 400（评分值无效）、404（运行不存在）。
    """
    if body.rating not in (1, -1):
        raise HTTPException(status_code=400, detail="rating must be +1 or -1")

    user_id = await get_current_user(request)

    # 验证运行存在且属于指定线程
    run_store = get_run_store(request)
    run = await run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if run.get("thread_id") != thread_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found in thread {thread_id}")

    feedback_repo = get_feedback_repo(request)
    return await feedback_repo.upsert(
        run_id=run_id,
        thread_id=thread_id,
        rating=body.rating,
        user_id=user_id,
        comment=body.comment,
    )


@router.delete("/{thread_id}/runs/{run_id}/feedback")
@require_permission("threads", "delete", owner_check=True, require_existing=True)
async def delete_run_feedback(
    thread_id: str,
    run_id: str,
    request: Request,
) -> dict[str, bool]:
    """删除当前用户对指定运行的所有反馈。

    Args:
        thread_id: 线程 ID。
        run_id: 运行 ID。
        request: FastAPI 请求对象。

    Returns:
        包含 success=True 的字典。

    Raises:
        HTTPException: 状态码 404（无反馈记录）。
    """
    user_id = await get_current_user(request)
    feedback_repo = get_feedback_repo(request)
    deleted = await feedback_repo.delete_by_run(
        thread_id=thread_id,
        run_id=run_id,
        user_id=user_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="No feedback found for this run")
    return {"success": True}


@router.post("/{thread_id}/runs/{run_id}/feedback", response_model=FeedbackResponse)
@require_permission("threads", "write", owner_check=True, require_existing=True)
async def create_feedback(
    thread_id: str,
    run_id: str,
    body: FeedbackCreateRequest,
    request: Request,
) -> dict[str, Any]:
    """为指定运行提交新的反馈（thumbs-up/down）。

    可选地将反馈关联到特定消息（通过 message_id）。

    Args:
        thread_id: 线程 ID。
        run_id: 运行 ID。
        body: 反馈创建请求体。
        request: FastAPI 请求对象。

    Returns:
        新创建的反馈记录。

    Raises:
        HTTPException: 状态码 400（评分值无效）、404（运行不存在）。
    """
    if body.rating not in (1, -1):
        raise HTTPException(status_code=400, detail="rating must be +1 or -1")

    user_id = await get_current_user(request)

    # 验证运行存在且属于指定线程
    run_store = get_run_store(request)
    run = await run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if run.get("thread_id") != thread_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found in thread {thread_id}")

    feedback_repo = get_feedback_repo(request)
    return await feedback_repo.create(
        run_id=run_id,
        thread_id=thread_id,
        rating=body.rating,
        user_id=user_id,
        message_id=body.message_id,
        comment=body.comment,
    )


@router.get("/{thread_id}/runs/{run_id}/feedback", response_model=list[FeedbackResponse])
@require_permission("threads", "read", owner_check=True)
async def list_feedback(
    thread_id: str,
    run_id: str,
    request: Request,
) -> list[dict[str, Any]]:
    """列出指定运行的所有反馈记录。

    Args:
        thread_id: 线程 ID。
        run_id: 运行 ID。
        request: FastAPI 请求对象。

    Returns:
        反馈记录列表。
    """
    feedback_repo = get_feedback_repo(request)
    return await feedback_repo.list_by_run(thread_id, run_id)


@router.get("/{thread_id}/runs/{run_id}/feedback/stats", response_model=FeedbackStatsResponse)
@require_permission("threads", "read", owner_check=True)
async def feedback_stats(
    thread_id: str,
    run_id: str,
    request: Request,
) -> dict[str, Any]:
    """获取指定运行的反馈聚合统计（正面/负面计数）。

    Args:
        thread_id: 线程 ID。
        run_id: 运行 ID。
        request: FastAPI 请求对象。

    Returns:
        包含 total、positive、negative 计数的统计结果。
    """
    feedback_repo = get_feedback_repo(request)
    return await feedback_repo.aggregate_by_run(thread_id, run_id)


@router.delete("/{thread_id}/runs/{run_id}/feedback/{feedback_id}")
@require_permission("threads", "delete", owner_check=True, require_existing=True)
async def delete_feedback(
    thread_id: str,
    run_id: str,
    feedback_id: str,
    request: Request,
) -> dict[str, bool]:
    """按反馈 ID 精确删除单条反馈记录。

    删除前验证反馈确实属于指定的线程和运行，防止越权删除。

    Args:
        thread_id: 线程 ID。
        run_id: 运行 ID。
        feedback_id: 反馈 ID。
        request: FastAPI 请求对象。

    Returns:
        包含 success=True 的字典。

    Raises:
        HTTPException: 状态码 404（反馈不存在或不属于指定运行）。
    """
    feedback_repo = get_feedback_repo(request)
    # 删除前验证反馈归属关系，防止越权操作
    existing = await feedback_repo.get(feedback_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Feedback {feedback_id} not found")
    if existing.get("thread_id") != thread_id or existing.get("run_id") != run_id:
        raise HTTPException(status_code=404, detail=f"Feedback {feedback_id} not found in run {run_id}")
    deleted = await feedback_repo.delete(feedback_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Feedback {feedback_id} not found")
    return {"success": True}
