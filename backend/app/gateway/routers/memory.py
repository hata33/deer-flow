"""全局记忆（Memory）数据管理路由。

本模块实现了 DeerFlow 记忆系统的完整 HTTP API，允许用户和前端
直接查询、修改和管理 AI 智能体的长期记忆数据。

记忆系统架构：
- 记忆数据以 JSON 格式持久化存储在文件系统中
- 每个用户拥有独立的记忆存储空间（通过 user_id 隔离）
- 记忆数据包含三大部分：用户上下文、历史上下文、事实列表

核心功能：
- 读取/重载/清除记忆数据
- 事实（Fact）的创建、删除、部分更新
- 记忆数据的导入/导出（JSON 格式）
- 记忆系统配置查询
- 记忆状态查询（配置 + 数据，单次请求获取全部）

数据模型：
- UserContext: 用户上下文（工作、个人、关注点）
- HistoryContext: 历史上下文（近期、早期、长期背景）
- Fact: 记忆事实条目（ID、内容、类别、置信度、来源）

路由前缀：/api
标签：memory
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from deerflow.agents.memory.updater import (
    clear_memory_data,
    create_memory_fact,
    delete_memory_fact,
    get_memory_data,
    import_memory_data,
    reload_memory_data,
    update_memory_fact,
)
from deerflow.config.memory_config import get_memory_config
from deerflow.runtime.user_context import get_effective_user_id

router = APIRouter(prefix="/api", tags=["memory"])


class ContextSection(BaseModel):
    """上下文段落模型（用于用户上下文和历史上下文的各子项）。

    Attributes:
        summary: 段落摘要内容。
        updatedAt: 最后更新时间戳。
    """

    summary: str = Field(default="", description="Summary content")
    updatedAt: str = Field(default="", description="Last update timestamp")


class UserContext(BaseModel):
    """用户上下文模型，描述用户的当前状态和偏好。

    Attributes:
        workContext: 工作相关的上下文信息。
        personalContext: 个人偏好和特征。
        topOfMind: 当前关注重点。
    """

    workContext: ContextSection = Field(default_factory=ContextSection)
    personalContext: ContextSection = Field(default_factory=ContextSection)
    topOfMind: ContextSection = Field(default_factory=ContextSection)


class HistoryContext(BaseModel):
    """历史上下文模型，描述用户的长期行为模式。

    Attributes:
        recentMonths: 近期活动摘要。
        earlierContext: 早期背景信息。
        longTermBackground: 长期行为模式。
    """

    recentMonths: ContextSection = Field(default_factory=ContextSection)
    earlierContext: ContextSection = Field(default_factory=ContextSection)
    longTermBackground: ContextSection = Field(default_factory=ContextSection)


class Fact(BaseModel):
    """记忆事实条目模型。

    每个事实代表从对话中提取的一条关键信息，带有置信度和来源追踪。

    Attributes:
        id: 事实唯一标识符。
        content: 事实内容文本。
        category: 事实类别（如 "preference"、"context" 等）。
        confidence: 置信度分数（0-1）。
        createdAt: 创建时间戳。
        source: 来源线程 ID。
        sourceError: 可选的错误描述（记录之前的错误方法或错误途径）。
    """

    id: str = Field(..., description="Unique identifier for the fact")
    content: str = Field(..., description="Fact content")
    category: str = Field(default="context", description="Fact category")
    confidence: float = Field(default=0.5, description="Confidence score (0-1)")
    createdAt: str = Field(default="", description="Creation timestamp")
    source: str = Field(default="unknown", description="Source thread ID")
    sourceError: str | None = Field(default=None, description="Optional description of the prior mistake or wrong approach")


class MemoryResponse(BaseModel):
    """记忆数据响应模型，包含完整的记忆结构。

    Attributes:
        version: 记忆数据格式版本号。
        lastUpdated: 最后更新时间戳。
        user: 用户上下文信息。
        history: 历史上下文信息。
        facts: 记忆事实列表。
    """

    version: str = Field(default="1.0", description="Memory schema version")
    lastUpdated: str = Field(default="", description="Last update timestamp")
    user: UserContext = Field(default_factory=UserContext)
    history: HistoryContext = Field(default_factory=HistoryContext)
    facts: list[Fact] = Field(default_factory=list)


def _map_memory_fact_value_error(exc: ValueError) -> HTTPException:
    """将记忆更新器中的验证错误转换为稳定的 API 响应。

    根据异常参数区分不同类型的验证失败，返回对应的错误描述。

    Args:
        exc: 记忆更新器抛出的 ValueError。

    Returns:
        包含具体错误描述的 HTTPException（状态码 400）。
    """
    if exc.args and exc.args[0] == "confidence":
        detail = "Invalid confidence value; must be between 0 and 1."
    else:
        detail = "Memory fact content cannot be empty."
    return HTTPException(status_code=400, detail=detail)


class FactCreateRequest(BaseModel):
    """创建记忆事实的请求模型。

    Attributes:
        content: 事实内容（至少 1 个字符）。
        category: 事实类别。
        confidence: 置信度分数（0-1）。
    """

    content: str = Field(..., min_length=1, description="Fact content")
    category: str = Field(default="context", description="Fact category")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="Confidence score (0-1)")


class FactPatchRequest(BaseModel):
    """部分更新记忆事实的请求模型。

    省略的字段保持原值不变，仅更新显式传入的字段。

    Attributes:
        content: 新的事实内容（可选）。
        category: 新的类别（可选）。
        confidence: 新的置信度（可选）。
    """

    content: str | None = Field(default=None, min_length=1, description="Fact content")
    category: str | None = Field(default=None, description="Fact category")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0, description="Confidence score (0-1)")


class MemoryConfigResponse(BaseModel):
    """记忆系统配置响应模型。

    Attributes:
        enabled: 记忆功能是否启用。
        storage_path: 记忆存储文件路径。
        debounce_seconds: 记忆更新的防抖时间。
        max_facts: 最大事实存储数量。
        fact_confidence_threshold: 事实的最低置信度阈值。
        injection_enabled: 记忆注入功能是否启用。
        max_injection_tokens: 记忆注入的最大令牌数。
    """

    enabled: bool = Field(..., description="Whether memory is enabled")
    storage_path: str = Field(..., description="Path to memory storage file")
    debounce_seconds: int = Field(..., description="Debounce time for memory updates")
    max_facts: int = Field(..., description="Maximum number of facts to store")
    fact_confidence_threshold: float = Field(..., description="Minimum confidence threshold for facts")
    injection_enabled: bool = Field(..., description="Whether memory injection is enabled")
    max_injection_tokens: int = Field(..., description="Maximum tokens for memory injection")


class MemoryStatusResponse(BaseModel):
    """记忆系统状态响应模型（配置 + 数据）。

    Attributes:
        config: 记忆系统配置。
        data: 当前记忆数据。
    """

    config: MemoryConfigResponse
    data: MemoryResponse


@router.get(
    "/memory",
    response_model=MemoryResponse,
    response_model_exclude_none=True,
    summary="Get Memory Data",
    description="Retrieve the current global memory data including user context, history, and facts.",
)
async def get_memory() -> MemoryResponse:
    """获取当前用户的完整记忆数据。

    返回用户上下文、历史上下文和记忆事实列表。

    Returns:
        MemoryResponse，包含完整的记忆结构数据。
    """
    memory_data = get_memory_data(user_id=get_effective_user_id())
    return MemoryResponse(**memory_data)


@router.post(
    "/memory/reload",
    response_model=MemoryResponse,
    response_model_exclude_none=True,
    summary="Reload Memory Data",
    description="Reload memory data from the storage file, refreshing the in-memory cache.",
)
async def reload_memory() -> MemoryResponse:
    """从存储文件重新加载记忆数据。

    强制从磁盘文件重新读取记忆数据并刷新内存缓存，
    适用于文件被外部修改后需要同步的场景。

    Returns:
        MemoryResponse，重新加载后的记忆数据。
    """
    memory_data = reload_memory_data(user_id=get_effective_user_id())
    return MemoryResponse(**memory_data)


@router.delete(
    "/memory",
    response_model=MemoryResponse,
    response_model_exclude_none=True,
    summary="Clear All Memory Data",
    description="Delete all saved memory data and reset the memory structure to an empty state.",
)
async def clear_memory() -> MemoryResponse:
    """清除所有已持久化的记忆数据。

    将记忆结构重置为空白状态。

    Returns:
        MemoryResponse，重置后的空白记忆数据。

    Raises:
        HTTPException: 状态码 500，当清除操作失败时抛出。
    """
    try:
        memory_data = clear_memory_data(user_id=get_effective_user_id())
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to clear memory data.") from exc

    return MemoryResponse(**memory_data)


@router.post(
    "/memory/facts",
    response_model=MemoryResponse,
    response_model_exclude_none=True,
    summary="Create Memory Fact",
    description="Create a single saved memory fact manually.",
)
async def create_memory_fact_endpoint(request: FactCreateRequest) -> MemoryResponse:
    """手动创建单条记忆事实。

    Args:
        request: 事实创建请求体。

    Returns:
        MemoryResponse，包含新创建事实后的完整记忆数据。

    Raises:
        HTTPException: 状态码 400（验证失败）或 500（写入失败）。
    """
    try:
        memory_data = create_memory_fact(
            content=request.content,
            category=request.category,
            confidence=request.confidence,
            user_id=get_effective_user_id(),
        )
    except ValueError as exc:
        raise _map_memory_fact_value_error(exc) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to create memory fact.") from exc

    return MemoryResponse(**memory_data)


@router.delete(
    "/memory/facts/{fact_id}",
    response_model=MemoryResponse,
    response_model_exclude_none=True,
    summary="Delete Memory Fact",
    description="Delete a single saved memory fact by its fact id.",
)
async def delete_memory_fact_endpoint(fact_id: str) -> MemoryResponse:
    """按 ID 删除单条记忆事实。

    Args:
        fact_id: 待删除事实的唯一标识符。

    Returns:
        MemoryResponse，删除后的完整记忆数据。

    Raises:
        HTTPException: 状态码 404（事实不存在）或 500（删除失败）。
    """
    try:
        memory_data = delete_memory_fact(fact_id, user_id=get_effective_user_id())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Memory fact '{fact_id}' not found.") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to delete memory fact.") from exc

    return MemoryResponse(**memory_data)


@router.patch(
    "/memory/facts/{fact_id}",
    response_model=MemoryResponse,
    response_model_exclude_none=True,
    summary="Patch Memory Fact",
    description="Partially update a single saved memory fact by its fact id while preserving omitted fields.",
)
async def update_memory_fact_endpoint(fact_id: str, request: FactPatchRequest) -> MemoryResponse:
    """部分更新单条记忆事实。

    仅更新请求体中显式传入的字段，省略的字段保持原值不变。

    Args:
        fact_id: 待更新事实的唯一标识符。
        request: 部分更新请求体。

    Returns:
        MemoryResponse，更新后的完整记忆数据。

    Raises:
        HTTPException: 状态码 400（验证失败）、404（事实不存在）或 500（更新失败）。
    """
    try:
        memory_data = update_memory_fact(
            fact_id=fact_id,
            content=request.content,
            category=request.category,
            confidence=request.confidence,
            user_id=get_effective_user_id(),
        )
    except ValueError as exc:
        raise _map_memory_fact_value_error(exc) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Memory fact '{fact_id}' not found.") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to update memory fact.") from exc

    return MemoryResponse(**memory_data)


@router.get(
    "/memory/export",
    response_model=MemoryResponse,
    response_model_exclude_none=True,
    summary="Export Memory Data",
    description="Export the current global memory data as JSON for backup or transfer.",
)
async def export_memory() -> MemoryResponse:
    """导出当前记忆数据为 JSON 格式。

    用于备份或跨系统迁移。

    Returns:
        MemoryResponse，完整的记忆数据快照。
    """
    memory_data = get_memory_data(user_id=get_effective_user_id())
    return MemoryResponse(**memory_data)


@router.post(
    "/memory/import",
    response_model=MemoryResponse,
    response_model_exclude_none=True,
    summary="Import Memory Data",
    description="Import and overwrite the current global memory data from a JSON payload.",
)
async def import_memory(request: MemoryResponse) -> MemoryResponse:
    """导入并覆盖当前记忆数据。

    使用提供的 JSON 数据完全替换现有记忆数据。

    Args:
        request: 包含完整记忆结构的请求体。

    Returns:
        MemoryResponse，导入后的记忆数据。

    Raises:
        HTTPException: 状态码 500，当导入操作失败时抛出。
    """
    try:
        memory_data = import_memory_data(request.model_dump(), user_id=get_effective_user_id())
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to import memory data.") from exc

    return MemoryResponse(**memory_data)


@router.get(
    "/memory/config",
    response_model=MemoryConfigResponse,
    summary="Get Memory Configuration",
    description="Retrieve the current memory system configuration.",
)
async def get_memory_config_endpoint() -> MemoryConfigResponse:
    """获取记忆系统的当前配置。

    Returns:
        MemoryConfigResponse，包含记忆系统的各项配置参数。
    """
    config = get_memory_config()
    return MemoryConfigResponse(
        enabled=config.enabled,
        storage_path=config.storage_path,
        debounce_seconds=config.debounce_seconds,
        max_facts=config.max_facts,
        fact_confidence_threshold=config.fact_confidence_threshold,
        injection_enabled=config.injection_enabled,
        max_injection_tokens=config.max_injection_tokens,
    )


@router.get(
    "/memory/status",
    response_model=MemoryStatusResponse,
    response_model_exclude_none=True,
    summary="Get Memory Status",
    description="Retrieve both memory configuration and current data in a single request.",
)
async def get_memory_status() -> MemoryStatusResponse:
    """获取记忆系统的完整状态（配置 + 数据）。

    单次请求同时返回配置信息和当前记忆数据，
    减少前端初始化时的网络请求次数。

    Returns:
        MemoryStatusResponse，包含配置和数据的组合响应。
    """
    config = get_memory_config()
    memory_data = get_memory_data(user_id=get_effective_user_id())

    return MemoryStatusResponse(
        config=MemoryConfigResponse(
            enabled=config.enabled,
            storage_path=config.storage_path,
            debounce_seconds=config.debounce_seconds,
            max_facts=config.max_facts,
            fact_confidence_threshold=config.fact_confidence_threshold,
            injection_enabled=config.injection_enabled,
            max_injection_tokens=config.max_injection_tokens,
        ),
        data=MemoryResponse(**memory_data),
    )
