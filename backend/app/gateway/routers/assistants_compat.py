"""LangGraph Assistants 协议兼容层路由。

本模块提供了一组与 LangGraph Platform assistants API 兼容的端点，
使前端可以使用 LangGraph SDK 的 ``useStream`` React hook 无缝对接
DeerFlow 后端。这是最小化的桩实现，仅满足 hook 初始化所需的接口：

- ``assistants.search()`` — 搜索/列出可用智能体
- ``assistants.get()`` — 获取单个智能体详情

底层实现：
- 默认智能体为 DeerFlow 的 lead_agent（主编排智能体）
- 同时从 config.yaml 的 agents 目录加载用户自定义智能体
- 所有智能体共享同一个 LangGraph 图（lead_agent），差异通过
  配置和 SOUL.md 实现

附加端点：
- ``GET /{assistant_id}/graph`` — 返回最小图结构描述（SDK 验证用）
- ``GET /{assistant_id}/schemas`` — 返回空 JSON Schema（网关不支持完整内省）

路由前缀：/api/assistants
标签：assistants-compat
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/assistants", tags=["assistants-compat"])


class AssistantResponse(BaseModel):
    """LangGraph Platform 兼容的智能体响应模型。

    Attributes:
        assistant_id: 智能体唯一标识符。
        graph_id: 关联的 LangGraph 图 ID。
        name: 智能体显示名称。
        config: 运行时配置字典。
        metadata: 元数据字典（如创建者信息）。
        description: 智能体描述文本。
        created_at: ISO 格式创建时间。
        updated_at: ISO 格式更新时间。
        version: 版本号。
    """

    assistant_id: str
    graph_id: str
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    created_at: str = ""
    updated_at: str = ""
    version: int = 1


class AssistantSearchRequest(BaseModel):
    """智能体搜索请求模型。

    Attributes:
        graph_id: 按图 ID 过滤（可选）。
        name: 按名称模糊搜索（可选）。
        metadata: 按元数据过滤（可选）。
        limit: 返回结果数量上限。
        offset: 分页偏移量。
    """

    graph_id: str | None = None
    name: str | None = None
    metadata: dict[str, Any] | None = None
    limit: int = 10
    offset: int = 0


def _get_default_assistant() -> AssistantResponse:
    """返回默认的 lead_agent 智能体响应对象。

    作为 DeerFlow 的主编排智能体，lead_agent 始终可用，
    无需依赖外部配置。

    Returns:
        默认智能体的 AssistantResponse 对象。
    """
    now = datetime.now(UTC).isoformat()
    return AssistantResponse(
        assistant_id="lead_agent",
        graph_id="lead_agent",
        name="lead_agent",
        config={},
        metadata={"created_by": "system"},
        description="DeerFlow lead agent",
        created_at=now,
        updated_at=now,
        version=1,
    )


def _list_assistants() -> list[AssistantResponse]:
    """列出所有可用的智能体（系统默认 + 用户自定义）。

    从 config.yaml 的 agents 目录加载自定义智能体配置，
    并将其映射为 AssistantResponse 格式。加载失败时静默降级。

    Returns:
        包含默认智能体和自定义智能体的列表。
    """
    assistants = [_get_default_assistant()]

    # 同时包含 config.yaml agents 目录中的自定义智能体
    try:
        from deerflow.config.agents_config import list_custom_agents

        for agent_cfg in list_custom_agents():
            now = datetime.now(UTC).isoformat()
            assistants.append(
                AssistantResponse(
                    assistant_id=agent_cfg.name,
                    graph_id="lead_agent",  # 所有智能体共用同一个图
                    name=agent_cfg.name,
                    config={},
                    metadata={"created_by": "user"},
                    description=agent_cfg.description or "",
                    created_at=now,
                    updated_at=now,
                    version=1,
                )
            )
    except Exception:
        logger.debug("Could not load custom agents for assistants list")

    return assistants


@router.post("/search", response_model=list[AssistantResponse])
async def search_assistants(body: AssistantSearchRequest | None = None) -> list[AssistantResponse]:
    """搜索智能体。

    支持按 graph_id 精确过滤和按 name 模糊搜索，
    结果支持分页（offset/limit）。

    Args:
        body: 搜索请求体，可选。

    Returns:
        匹配的智能体列表（分页后）。
    """
    assistants = _list_assistants()

    # 按 graph_id 精确过滤
    if body and body.graph_id:
        assistants = [a for a in assistants if a.graph_id == body.graph_id]
    # 按 name 模糊搜索（大小写不敏感）
    if body and body.name:
        assistants = [a for a in assistants if body.name.lower() in a.name.lower()]

    offset = body.offset if body else 0
    limit = body.limit if body else 10
    return assistants[offset : offset + limit]


@router.get("/{assistant_id}", response_model=AssistantResponse)
async def get_assistant_compat(assistant_id: str) -> AssistantResponse:
    """根据 ID 获取单个智能体。

    Args:
        assistant_id: 智能体 ID。

    Returns:
        匹配的智能体详情。

    Raises:
        HTTPException: 状态码 404，当智能体不存在时抛出。
    """
    for a in _list_assistants():
        if a.assistant_id == assistant_id:
            return a
    raise HTTPException(status_code=404, detail=f"Assistant {assistant_id} not found")


@router.get("/{assistant_id}/graph")
async def get_assistant_graph(assistant_id: str) -> dict:
    """获取智能体关联的图结构。

    返回最小图描述。完整的图内省在 Gateway 中不受支持——
    此桩实现仅满足 SDK 验证需求。

    Args:
        assistant_id: 智能体 ID。

    Returns:
        包含 graph_id、nodes 和 edges 的字典。

    Raises:
        HTTPException: 状态码 404，当智能体不存在时抛出。
    """
    found = any(a.assistant_id == assistant_id for a in _list_assistants())
    if not found:
        raise HTTPException(status_code=404, detail=f"Assistant {assistant_id} not found")

    return {
        "graph_id": "lead_agent",
        "nodes": [],
        "edges": [],
    }


@router.get("/{assistant_id}/schemas")
async def get_assistant_schemas(assistant_id: str) -> dict:
    """获取智能体的输入/输出/状态 JSON Schema。

    返回空 Schema——Gateway 不支持完整的类型内省。

    Args:
        assistant_id: 智能体 ID。

    Returns:
        包含各 Schema 空字典的响应。

    Raises:
        HTTPException: 状态码 404，当智能体不存在时抛出。
    """
    found = any(a.assistant_id == assistant_id for a in _list_assistants())
    if not found:
        raise HTTPException(status_code=404, detail=f"Assistant {assistant_id} not found")

    return {
        "graph_id": "lead_agent",
        "input_schema": {},
        "output_schema": {},
        "state_schema": {},
        "config_schema": {},
    }
