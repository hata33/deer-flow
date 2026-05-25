"""线程（Thread）的生命周期管理路由。

本模块实现了线程的完整 CRUD 和状态管理，结合了线程本地文件系统清理
与 LangGraph Platform 兼容的线程管理接口。线程是 DeerFlow 中对话
的基本单元，每个线程包含独立的对话历史、检查点和元数据。

核心端点：
- DELETE /{thread_id} — 删除线程及其所有关联数据
- POST / — 创建新线程（幂等）
- POST /search — 搜索/列出线程
- PATCH /{thread_id} — 更新线程元数据
- GET /{thread_id} — 获取线程信息
- GET /{thread_id}/state — 获取线程状态快照
- POST /{thread_id}/state — 更新线程状态（人机交互恢复、标题重命名）
- POST /{thread_id}/history — 获取检查点历史

数据模型：
- 线程状态序列化通过 serialize_channel_values 实现，确保 LangChain
  消息对象转换为 JSON 安全的字典格式，匹配 LangGraph Platform 协议
- 元数据中包含 owner_id 和 user_id 等服务器保留字段，客户端不可设置

安全机制：
- 服务器保留元数据字段在入站时被自动剥离
- 元数据过滤器验证防止 SQL 注入

路由前缀：/api/threads
标签：threads
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from langgraph.checkpoint.base import empty_checkpoint
from pydantic import BaseModel, Field, field_validator

from app.gateway.authz import require_permission
from app.gateway.deps import get_checkpointer
from app.gateway.utils import sanitize_log_param
from deerflow.config.paths import Paths, get_paths
from deerflow.runtime import serialize_channel_values
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.utils.time import coerce_iso, now_iso

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/threads", tags=["threads"])


# 服务器控制的元数据键，客户端不允许设置。
# Pydantic field_validator("metadata") 在每个入站模型上剥离这些键，
# 防止恶意客户端通过 API 表面伪造所有者身份。
# 这是纵深防御——行级不变量仍由 threads_meta.user_id 从认证上下文变量填充，
# 此列表关闭了元数据 blob 回显漏洞。
_SERVER_RESERVED_METADATA_KEYS: frozenset[str] = frozenset({"owner_id", "user_id"})


def _strip_reserved_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """返回移除了服务器保留键的元数据副本。

    Args:
        metadata: 原始元数据字典。

    Returns:
        清理后的元数据字典。
    """
    if not metadata:
        return metadata or {}
    return {k: v for k, v in metadata.items() if k not in _SERVER_RESERVED_METADATA_KEYS}


# ---------------------------------------------------------------------------
# 响应/请求模型
# ---------------------------------------------------------------------------


class ThreadDeleteResponse(BaseModel):
    """线程删除响应模型。

    Attributes:
        success: 删除是否成功。
        message: 结果描述消息。
    """

    success: bool
    message: str


class ThreadResponse(BaseModel):
    """单个线程的响应模型。

    Attributes:
        thread_id: 线程唯一标识符。
        status: 线程状态（idle、busy、interrupted、error）。
        created_at: ISO 格式创建时间。
        updated_at: ISO 格式更新时间。
        metadata: 线程元数据。
        values: 当前状态通道值。
        interrupts: 待处理的中断信息。
    """

    thread_id: str = Field(description="Unique thread identifier")
    status: str = Field(default="idle", description="Thread status: idle, busy, interrupted, error")
    created_at: str = Field(default="", description="ISO timestamp")
    updated_at: str = Field(default="", description="ISO timestamp")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Thread metadata")
    values: dict[str, Any] = Field(default_factory=dict, description="Current state channel values")
    interrupts: dict[str, Any] = Field(default_factory=dict, description="Pending interrupts")


class ThreadCreateRequest(BaseModel):
    """创建线程的请求体。

    Attributes:
        thread_id: 可选的线程 ID（省略时自动生成）。
        assistant_id: 关联的智能体 ID。
        metadata: 初始元数据。
    """

    thread_id: str | None = Field(default=None, description="Optional thread ID (auto-generated if omitted)")
    assistant_id: str | None = Field(default=None, description="Associate thread with an assistant")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Initial metadata")

    _strip_reserved = field_validator("metadata")(classmethod(lambda cls, v: _strip_reserved_metadata(v)))


class ThreadSearchRequest(BaseModel):
    """搜索线程的请求体。

    Attributes:
        metadata: 元数据精确匹配过滤器。
        limit: 最大返回数量。
        offset: 分页偏移量。
        status: 按状态过滤。
    """

    metadata: dict[str, Any] = Field(default_factory=dict, description="Metadata filter (exact match)")
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum results")
    offset: int = Field(default=0, ge=0, description="Pagination offset")
    status: str | None = Field(default=None, description="Filter by thread status")

    @field_validator("metadata")
    @classmethod
    def _validate_metadata_filters(cls, v: dict[str, Any]) -> dict[str, Any]:
        """拒绝 SQL 后端无法编译的过滤条目。

        确保 SQL 和内存后端的行为一致。
        参见 deerflow.persistence.json_compat 中的共享验证器。
        """
        if not v:
            return v
        from deerflow.persistence.json_compat import validate_metadata_filter_key, validate_metadata_filter_value

        bad_entries: list[str] = []
        for key, value in v.items():
            if not validate_metadata_filter_key(key):
                bad_entries.append(f"{key!r} (unsafe key)")
            elif not validate_metadata_filter_value(value):
                bad_entries.append(f"{key!r} (unsupported value type {type(value).__name__})")
        if bad_entries:
            raise ValueError(f"Invalid metadata filter entries: {', '.join(bad_entries)}")
        return v


class ThreadStateResponse(BaseModel):
    """线程状态响应模型。

    Attributes:
        values: 当前通道值。
        next: 下一步要执行的任务列表。
        metadata: 检查点元数据。
        checkpoint: 检查点信息。
        checkpoint_id: 当前检查点 ID。
        parent_checkpoint_id: 父检查点 ID。
        created_at: 检查点时间戳。
        tasks: 中断任务的详细信息。
    """

    values: dict[str, Any] = Field(default_factory=dict, description="Current channel values")
    next: list[str] = Field(default_factory=list, description="Next tasks to execute")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Checkpoint metadata")
    checkpoint: dict[str, Any] = Field(default_factory=dict, description="Checkpoint info")
    checkpoint_id: str | None = Field(default=None, description="Current checkpoint ID")
    parent_checkpoint_id: str | None = Field(default=None, description="Parent checkpoint ID")
    created_at: str | None = Field(default=None, description="Checkpoint timestamp")
    tasks: list[dict[str, Any]] = Field(default_factory=list, description="Interrupted task details")


class ThreadPatchRequest(BaseModel):
    """更新线程元数据的请求体。

    Attributes:
        metadata: 要合并的元数据。
    """

    metadata: dict[str, Any] = Field(default_factory=dict, description="Metadata to merge")

    _strip_reserved = field_validator("metadata")(classmethod(lambda cls, v: _strip_reserved_metadata(v)))


class ThreadStateUpdateRequest(BaseModel):
    """更新线程状态的请求体（用于人机交互恢复）。

    Attributes:
        values: 要合并的通道值。
        checkpoint_id: 从指定检查点分支。
        checkpoint: 完整检查点对象。
        as_node: 更新的节点身份。
    """

    values: dict[str, Any] | None = Field(default=None, description="Channel values to merge")
    checkpoint_id: str | None = Field(default=None, description="Checkpoint to branch from")
    checkpoint: dict[str, Any] | None = Field(default=None, description="Full checkpoint object")
    as_node: str | None = Field(default=None, description="Node identity for the update")


class HistoryEntry(BaseModel):
    """单个检查点历史条目。

    Attributes:
        checkpoint_id: 检查点 ID。
        parent_checkpoint_id: 父检查点 ID。
        metadata: 元数据。
        values: 通道值快照。
        created_at: 创建时间。
        next: 下一步任务。
    """

    checkpoint_id: str
    parent_checkpoint_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    next: list[str] = Field(default_factory=list)


class ThreadHistoryRequest(BaseModel):
    """检查点历史查询请求体。

    Attributes:
        limit: 最大返回条目数。
        before: 分页游标。
    """

    limit: int = Field(default=10, ge=1, le=100, description="Maximum entries")
    before: str | None = Field(default=None, description="Cursor for pagination")


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _delete_thread_data(thread_id: str, paths: Paths | None = None, *, user_id: str | None = None) -> ThreadDeleteResponse:
    """删除线程在本地文件系统上的持久化数据。

    Args:
        thread_id: 线程 ID。
        paths: 路径管理器实例（可选）。
        user_id: 用户 ID（用于用户级目录）。

    Returns:
        ThreadDeleteResponse，包含操作结果。

    Raises:
        HTTPException: 状态码 422（参数无效）或 500（删除失败）。
    """
    path_manager = paths or get_paths()
    try:
        path_manager.delete_thread_dir(thread_id, user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError:
        # 线程数据可能不存在于磁盘上，这并不致命
        logger.debug("No local thread data to delete for %s", sanitize_log_param(thread_id))
        return ThreadDeleteResponse(success=True, message=f"No local data for {thread_id}")
    except Exception as exc:
        logger.exception("Failed to delete thread data for %s", sanitize_log_param(thread_id))
        raise HTTPException(status_code=500, detail="Failed to delete local thread data.") from exc

    logger.info("Deleted local thread data for %s", sanitize_log_param(thread_id))
    return ThreadDeleteResponse(success=True, message=f"Deleted local thread data for {thread_id}")


def _derive_thread_status(checkpoint_tuple) -> str:
    """从检查点元数据推导线程状态。

    状态逻辑：
    - 存在 __error__ 写入 → "error"
    - 存在待处理任务 → "interrupted"
    - 其他 → "idle"

    Args:
        checkpoint_tuple: 检查点元组对象。

    Returns:
        线程状态字符串。
    """
    if checkpoint_tuple is None:
        return "idle"
    pending_writes = getattr(checkpoint_tuple, "pending_writes", None) or []

    # 检查是否存在错误写入
    for pw in pending_writes:
        if len(pw) >= 2 and pw[1] == "__error__":
            return "error"

    # 检查是否存在待处理任务（表示中断）
    tasks = getattr(checkpoint_tuple, "tasks", None)
    if tasks:
        return "interrupted"

    return "idle"


# ---------------------------------------------------------------------------
# 端点实现
# ---------------------------------------------------------------------------


@router.delete("/{thread_id}", response_model=ThreadDeleteResponse)
@require_permission("threads", "delete", owner_check=True, require_existing=True)
async def delete_thread_data(thread_id: str, request: Request) -> ThreadDeleteResponse:
    """删除线程及其所有关联数据。

    清理步骤：
    1. 删除 DeerFlow 管理的线程目录
    2. 删除检查点数据（尽力而为）
    3. 删除 thread_meta 行（确保搜索结果不再包含该线程）

    Args:
        thread_id: 线程 ID。
        request: FastAPI 请求对象。

    Returns:
        ThreadDeleteResponse，包含删除结果。
    """
    from app.gateway.deps import get_thread_store

    # 清理本地文件系统数据
    response = _delete_thread_data(thread_id, user_id=get_effective_user_id())

    # 删除检查点（尽力而为）
    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is not None:
        try:
            if hasattr(checkpointer, "adelete_thread"):
                await checkpointer.adelete_thread(thread_id)
        except Exception:
            logger.debug("Could not delete checkpoints for thread %s (not critical)", sanitize_log_param(thread_id))

    # 删除 thread_meta 行（尽力而为）——sqlite 后端必需，
    # 否则已删除的线程仍会出现在 /threads/search 结果中
    try:
        thread_store = get_thread_store(request)
        await thread_store.delete(thread_id)
    except Exception:
        logger.debug("Could not delete thread_meta for %s (not critical)", sanitize_log_param(thread_id))

    return response


@router.post("", response_model=ThreadResponse)
async def create_thread(body: ThreadCreateRequest, request: Request) -> ThreadResponse:
    """创建新线程。

    写入 thread_meta 记录（使线程出现在搜索结果中）和空检查点
    （使状态端点立即可用）。幂等操作：当 thread_id 已存在时
    返回现有记录。

    Args:
        body: 线程创建请求体。
        request: FastAPI 请求对象。

    Returns:
        ThreadResponse，新创建或已存在的线程信息。

    Raises:
        HTTPException: 状态码 500，当创建失败时抛出。
    """
    from app.gateway.deps import get_thread_store

    checkpointer = get_checkpointer(request)
    thread_store = get_thread_store(request)
    thread_id = body.thread_id or str(uuid.uuid4())
    now = now_iso()
    # body.metadata 已被 ThreadCreateRequest._strip_reserved 清理

    # 幂等：已存在时返回现有记录
    existing_record = await thread_store.get(thread_id)
    if existing_record is not None:
        return ThreadResponse(
            thread_id=thread_id,
            status=existing_record.get("status", "idle"),
            created_at=coerce_iso(existing_record.get("created_at", "")),
            updated_at=coerce_iso(existing_record.get("updated_at", "")),
            metadata=existing_record.get("metadata", {}),
        )

    # 写入 thread_meta 使线程立即可搜索
    try:
        await thread_store.create(
            thread_id,
            assistant_id=getattr(body, "assistant_id", None),
            metadata=body.metadata,
        )
    except Exception:
        logger.exception("Failed to write thread_meta for %s", sanitize_log_param(thread_id))
        raise HTTPException(status_code=500, detail="Failed to create thread")

    # 写入空检查点使状态端点立即可用
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    try:
        ckpt_metadata = {
            "step": -1,
            "source": "input",
            "writes": None,
            "parents": {},
            **body.metadata,
            "created_at": now,
        }
        await checkpointer.aput(config, empty_checkpoint(), ckpt_metadata, {})
    except Exception:
        logger.exception("Failed to create checkpoint for thread %s", sanitize_log_param(thread_id))
        raise HTTPException(status_code=500, detail="Failed to create thread")

    logger.info("Thread created: %s", sanitize_log_param(thread_id))
    return ThreadResponse(
        thread_id=thread_id,
        status="idle",
        created_at=now,
        updated_at=now,
        metadata=body.metadata,
    )


@router.post("/search", response_model=list[ThreadResponse])
async def search_threads(body: ThreadSearchRequest, request: Request) -> list[ThreadResponse]:
    """搜索和列出线程。

    委托给配置的 ThreadMetaStore 实现（sqlite/postgres 使用 SQL 后端，
    memory 模式使用 Store 后端）。

    Args:
        body: 搜索请求体。
        request: FastAPI 请求对象。

    Returns:
        匹配的线程列表。

    Raises:
        HTTPException: 状态码 400，当元数据过滤器无效时抛出。
    """
    from app.gateway.deps import get_thread_store
    from deerflow.persistence.thread_meta import InvalidMetadataFilterError

    repo = get_thread_store(request)
    try:
        rows = await repo.search(
            metadata=body.metadata or None,
            status=body.status,
            limit=body.limit,
            offset=body.offset,
        )
    except InvalidMetadataFilterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        ThreadResponse(
            thread_id=r["thread_id"],
            status=r.get("status", "idle"),
            # coerce_iso 修复 MemoryThreadMetaStore 历史上使用 time.time() 写入的
            # Unix 秒值；SQL 后端的行已经是 ISO 字符串，直接通过
            created_at=coerce_iso(r.get("created_at", "")),
            updated_at=coerce_iso(r.get("updated_at", "")),
            metadata=r.get("metadata", {}),
            values={"title": r["display_name"]} if r.get("display_name") else {},
            interrupts={},
        )
        for r in rows
    ]


@router.patch("/{thread_id}", response_model=ThreadResponse)
@require_permission("threads", "write", owner_check=True, require_existing=True)
async def patch_thread(thread_id: str, body: ThreadPatchRequest, request: Request) -> ThreadResponse:
    """合并元数据到线程记录。

    Args:
        thread_id: 线程 ID。
        body: 元数据更新请求体。
        request: FastAPI 请求对象。

    Returns:
        ThreadResponse，更新后的线程信息。

    Raises:
        HTTPException: 状态码 404（线程不存在）或 500（更新失败）。
    """
    from app.gateway.deps import get_thread_store

    thread_store = get_thread_store(request)
    record = await thread_store.get(thread_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")

    # body.metadata 已被 ThreadPatchRequest._strip_reserved 清理
    try:
        await thread_store.update_metadata(thread_id, body.metadata)
    except Exception:
        logger.exception("Failed to patch thread %s", sanitize_log_param(thread_id))
        raise HTTPException(status_code=500, detail="Failed to update thread")

    # 重新读取以获取合并后的元数据和刷新的 updated_at
    record = await thread_store.get(thread_id) or record
    return ThreadResponse(
        thread_id=thread_id,
        status=record.get("status", "idle"),
        created_at=coerce_iso(record.get("created_at", "")),
        updated_at=coerce_iso(record.get("updated_at", "")),
        metadata=record.get("metadata", {}),
    )


@router.get("/{thread_id}", response_model=ThreadResponse)
@require_permission("threads", "read", owner_check=True)
async def get_thread(thread_id: str, request: Request) -> ThreadResponse:
    """获取线程信息。

    从 ThreadMetaStore 读取元数据，从检查点推导准确的执行状态。
    对于 ThreadMetaStore 引入之前创建的旧线程，回退到仅使用检查点
    数据（向后兼容）。

    Args:
        thread_id: 线程 ID。
        request: FastAPI 请求对象。

    Returns:
        ThreadResponse，线程详细信息。

    Raises:
        HTTPException: 状态码 404（线程不存在）或 500（获取失败）。
    """
    from app.gateway.deps import get_thread_store

    thread_store = get_thread_store(request)
    checkpointer = get_checkpointer(request)

    record: dict | None = await thread_store.get(thread_id)

    # 从检查点推导准确的执行状态
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    try:
        checkpoint_tuple = await checkpointer.aget_tuple(config)
    except Exception:
        logger.exception("Failed to get checkpoint for thread %s", sanitize_log_param(thread_id))
        raise HTTPException(status_code=500, detail="Failed to get thread")

    if record is None and checkpoint_tuple is None:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")

    # 线程存在于检查点但不在 thread_meta 中（如 ThreadMetaStore 引入前的旧数据），
    # 从检查点元数据合成最小记录
    if record is None and checkpoint_tuple is not None:
        ckpt_meta = getattr(checkpoint_tuple, "metadata", {}) or {}
        record = {
            "thread_id": thread_id,
            "status": "idle",
            "created_at": coerce_iso(ckpt_meta.get("created_at", "")),
            "updated_at": coerce_iso(ckpt_meta.get("updated_at", ckpt_meta.get("created_at", ""))),
            "metadata": {k: v for k, v in ckpt_meta.items() if k not in ("created_at", "updated_at", "step", "source", "writes", "parents")},
        }

    if record is None:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")

    status = _derive_thread_status(checkpoint_tuple) if checkpoint_tuple is not None else record.get("status", "idle")
    checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {} if checkpoint_tuple is not None else {}
    channel_values = checkpoint.get("channel_values", {})

    return ThreadResponse(
        thread_id=thread_id,
        status=status,
        created_at=coerce_iso(record.get("created_at", "")),
        updated_at=coerce_iso(record.get("updated_at", "")),
        metadata=record.get("metadata", {}),
        values=serialize_channel_values(channel_values),
    )


# ---------------------------------------------------------------------------
@router.get("/{thread_id}/state", response_model=ThreadStateResponse)
@require_permission("threads", "read", owner_check=True)
async def get_thread_state(thread_id: str, request: Request) -> ThreadStateResponse:
    """获取线程的最新状态快照。

    通道值经过序列化处理，确保 LangChain 消息对象被转换为 JSON 安全的字典。

    Args:
        thread_id: 线程 ID。
        request: FastAPI 请求对象。

    Returns:
        ThreadStateResponse，线程状态快照。

    Raises:
        HTTPException: 状态码 404（线程不存在）或 500（获取失败）。
    """
    checkpointer = get_checkpointer(request)

    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    try:
        checkpoint_tuple = await checkpointer.aget_tuple(config)
    except Exception:
        logger.exception("Failed to get state for thread %s", sanitize_log_param(thread_id))
        raise HTTPException(status_code=500, detail="Failed to get thread state")

    if checkpoint_tuple is None:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")

    checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}
    metadata = getattr(checkpoint_tuple, "metadata", {}) or {}
    checkpoint_id = None
    ckpt_config = getattr(checkpoint_tuple, "config", {})
    if ckpt_config:
        checkpoint_id = ckpt_config.get("configurable", {}).get("checkpoint_id")

    channel_values = checkpoint.get("channel_values", {})

    # 获取父检查点 ID
    parent_config = getattr(checkpoint_tuple, "parent_config", None)
    parent_checkpoint_id = None
    if parent_config:
        parent_checkpoint_id = parent_config.get("configurable", {}).get("checkpoint_id")

    # 提取待处理任务信息
    tasks_raw = getattr(checkpoint_tuple, "tasks", []) or []
    next_tasks = [t.name for t in tasks_raw if hasattr(t, "name")]
    tasks = [{"id": getattr(t, "id", ""), "name": getattr(t, "name", "")} for t in tasks_raw]

    values = serialize_channel_values(channel_values)

    return ThreadStateResponse(
        values=values,
        next=next_tasks,
        metadata=metadata,
        checkpoint={"id": checkpoint_id, "ts": coerce_iso(metadata.get("created_at", ""))},
        checkpoint_id=checkpoint_id,
        parent_checkpoint_id=parent_checkpoint_id,
        created_at=coerce_iso(metadata.get("created_at", "")),
        tasks=tasks,
    )


@router.post("/{thread_id}/state", response_model=ThreadStateResponse)
@require_permission("threads", "write", owner_check=True, require_existing=True)
async def update_thread_state(thread_id: str, body: ThreadStateUpdateRequest, request: Request) -> ThreadStateResponse:
    """更新线程状态（用于人机交互恢复或标题重命名）。

    写入新的检查点，将 body.values 合并到最新通道值中，
    然后同步更新的 title 字段到 ThreadMetaStore，使
    /threads/search 立即反映变更（适用于 sqlite 和内存后端）。

    Args:
        thread_id: 线程 ID。
        body: 状态更新请求体。
        request: FastAPI 请求对象。

    Returns:
        ThreadStateResponse，更新后的状态。

    Raises:
        HTTPException: 状态码 404（线程不存在）或 500（更新失败）。
    """
    from app.gateway.deps import get_thread_store

    checkpointer = get_checkpointer(request)
    thread_store = get_thread_store(request)

    # 读取配置需要包含 checkpoint_ns（默认为空字符串，即根图命名空间）。
    # checkpoint_id 可选；省略时获取线程的最新检查点。
    read_config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    if body.checkpoint_id:
        read_config["configurable"]["checkpoint_id"] = body.checkpoint_id

    try:
        checkpoint_tuple = await checkpointer.aget_tuple(read_config)
    except Exception:
        logger.exception("Failed to get state for thread %s", sanitize_log_param(thread_id))
        raise HTTPException(status_code=500, detail="Failed to get thread state")

    if checkpoint_tuple is None:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")

    # 使用可变副本以避免意外修改缓存对象
    checkpoint: dict[str, Any] = dict(getattr(checkpoint_tuple, "checkpoint", {}) or {})
    metadata: dict[str, Any] = dict(getattr(checkpoint_tuple, "metadata", {}) or {})
    channel_values: dict[str, Any] = dict(checkpoint.get("channel_values", {}))

    if body.values:
        channel_values.update(body.values)

    checkpoint["channel_values"] = channel_values
    metadata["updated_at"] = now_iso()

    if body.as_node:
        metadata["source"] = "update"
        metadata["step"] = metadata.get("step", 0) + 1
        metadata["writes"] = {body.as_node: body.values}

    # 写入配置必须包含 checkpoint_ns，但不包含 checkpoint_id，
    # 以便 aput 生成新的检查点 ID
    write_config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    try:
        new_config = await checkpointer.aput(write_config, checkpoint, metadata, {})
    except Exception:
        logger.exception("Failed to update state for thread %s", sanitize_log_param(thread_id))
        raise HTTPException(status_code=500, detail="Failed to update thread state")

    new_checkpoint_id: str | None = None
    if isinstance(new_config, dict):
        new_checkpoint_id = new_config.get("configurable", {}).get("checkpoint_id")

    # 将标题变更同步到 ThreadMetaStore，使 /threads/search 立即反映
    if body.values and "title" in body.values:
        new_title = body.values["title"]
        if new_title:  # 跳过空字符串和 None
            try:
                await thread_store.update_display_name(thread_id, new_title)
            except Exception:
                logger.debug("Failed to sync title to thread_meta for %s (non-fatal)", sanitize_log_param(thread_id))

    return ThreadStateResponse(
        values=serialize_channel_values(channel_values),
        next=[],
        metadata=metadata,
        checkpoint_id=new_checkpoint_id,
        created_at=coerce_iso(metadata.get("created_at", "")),
    )


@router.post("/{thread_id}/history", response_model=list[HistoryEntry])
@require_permission("threads", "read", owner_check=True)
async def get_thread_history(thread_id: str, body: ThreadHistoryRequest, request: Request) -> list[HistoryEntry]:
    """获取线程的检查点历史。

    消息从检查点的通道值（权威来源）中读取，通过 serialize_channel_values
    序列化。仅最新的（第一个）检查点携带 messages 键，避免在每个条目中重复。

    Args:
        thread_id: 线程 ID。
        body: 历史查询请求体。
        request: FastAPI 请求对象。

    Returns:
        检查点历史条目列表。

    Raises:
        HTTPException: 状态码 500，当获取历史失败时抛出。
    """
    checkpointer = get_checkpointer(request)

    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if body.before:
        config["configurable"]["checkpoint_id"] = body.before

    entries: list[HistoryEntry] = []
    is_latest_checkpoint = True
    try:
        async for checkpoint_tuple in checkpointer.alist(config, limit=body.limit):
            ckpt_config = getattr(checkpoint_tuple, "config", {})
            parent_config = getattr(checkpoint_tuple, "parent_config", None)
            metadata = getattr(checkpoint_tuple, "metadata", {}) or {}
            checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}

            checkpoint_id = ckpt_config.get("configurable", {}).get("checkpoint_id", "")
            parent_id = None
            if parent_config:
                parent_id = parent_config.get("configurable", {}).get("checkpoint_id")

            channel_values = checkpoint.get("channel_values", {})

            # 从检查点通道值构建 values 字典
            values: dict[str, Any] = {}
            if title := channel_values.get("title"):
                values["title"] = title
            if thread_data := channel_values.get("thread_data"):
                values["thread_data"] = thread_data

            # 仅在最新检查点条目上附加消息，避免重复
            if is_latest_checkpoint:
                messages = channel_values.get("messages")
                if messages:
                    values["messages"] = serialize_channel_values({"messages": messages}).get("messages", [])
            is_latest_checkpoint = False

            # 提取下一步待执行任务
            tasks_raw = getattr(checkpoint_tuple, "tasks", []) or []
            next_tasks = [t.name for t in tasks_raw if hasattr(t, "name")]

            # 从元数据中移除 LangGraph 内部键
            user_meta = {k: v for k, v in metadata.items() if k not in ("created_at", "updated_at", "step", "source", "writes", "parents")}
            # 保留 step 用于排序上下文
            if "step" in metadata:
                user_meta["step"] = metadata["step"]

            entries.append(
                HistoryEntry(
                    checkpoint_id=checkpoint_id,
                    parent_checkpoint_id=parent_id,
                    metadata=user_meta,
                    values=values,
                    created_at=coerce_iso(metadata.get("created_at", "")),
                    next=next_tasks,
                )
            )
    except Exception:
        logger.exception("Failed to get history for thread %s", sanitize_log_param(thread_id))
        raise HTTPException(status_code=500, detail="Failed to get thread history")

    return entries
