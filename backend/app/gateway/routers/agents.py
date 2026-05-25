"""自定义智能体（Agent）的 CRUD API 路由。

本模块提供了一整套用于管理自定义智能体的 RESTful API，支持完整的
创建、读取、更新、删除生命周期。智能体由两部分组成：

1. config.yaml — 结构化配置文件，包含名称、描述、模型、工具组、技能白名单等
2. SOUL.md — 自由格式的 Markdown 文件，定义智能体的人设、行为准则和对话风格

此外，本模块还提供用户全局配置文件（USER.md）的读写接口，该文件会被
注入到所有自定义智能体的上下文中，用于描述用户的背景和偏好。

安全机制：
- 所有接口受 agents_api.enabled 配置开关保护，默认关闭
- 智能体名称仅允许字母、数字和连字符，防止路径遍历攻击
- 文件系统操作按用户隔离，支持遗留共享布局的兼容检测
- 更新操作区分"字段省略"与"显式设为 null"两种语义

路由前缀：/api
标签：agents
"""

import logging
import re
import shutil

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from deerflow.config.agents_api_config import get_agents_api_config
from deerflow.config.agents_config import AgentConfig, list_custom_agents, load_agent_config, load_agent_soul
from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import get_effective_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["agents"])

# 智能体名称合法性正则：仅允许字母、数字和连字符，防止目录遍历等安全问题
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")


class AgentResponse(BaseModel):
    """自定义智能体响应模型。

    Attributes:
        name: 智能体名称（连字符格式）。
        description: 智能体描述信息。
        model: 可选的模型覆盖配置。
        tool_groups: 可选的工具组白名单。
        skills: 可选的技能白名单（None 表示全部启用，空列表表示禁用所有）。
        soul: SOUL.md 文件的原始内容。
    """

    name: str = Field(..., description="Agent name (hyphen-case)")
    description: str = Field(default="", description="Agent description")
    model: str | None = Field(default=None, description="Optional model override")
    tool_groups: list[str] | None = Field(default=None, description="Optional tool group whitelist")
    skills: list[str] | None = Field(default=None, description="Optional skill whitelist (None=all, []=none)")
    soul: str | None = Field(default=None, description="SOUL.md content")


class AgentsListResponse(BaseModel):
    """自定义智能体列表响应模型。"""

    agents: list[AgentResponse]


class AgentCreateRequest(BaseModel):
    """创建自定义智能体的请求体。

    Attributes:
        name: 智能体名称，必须匹配 ^[A-Za-z0-9-]+$ 模式，存储时自动转小写。
        description: 智能体描述。
        model: 可选的模型覆盖。
        tool_groups: 可选的工具组白名单。
        skills: 可选的技能白名单（None 表示全部启用，空列表表示无技能）。
        soul: SOUL.md 内容，定义智能体的人设和行为守卫。
    """

    name: str = Field(..., description="Agent name (must match ^[A-Za-z0-9-]+$, stored as lowercase)")
    description: str = Field(default="", description="Agent description")
    model: str | None = Field(default=None, description="Optional model override")
    tool_groups: list[str] | None = Field(default=None, description="Optional tool group whitelist")
    skills: list[str] | None = Field(default=None, description="Optional skill whitelist (None=all enabled, []=none)")
    soul: str = Field(default="", description="SOUL.md content — agent personality and behavioral guardrails")


class AgentUpdateRequest(BaseModel):
    """更新自定义智能体的请求体。

    所有字段均为可选；仅传入需要更新的字段。
    使用 model_fields_set 区分"字段省略"与"显式设为 null"。

    Attributes:
        description: 更新后的描述。
        model: 更新后的模型覆盖。
        tool_groups: 更新后的工具组白名单。
        skills: 更新后的技能白名单。
        soul: 更新后的 SOUL.md 内容。
    """

    description: str | None = Field(default=None, description="Updated description")
    model: str | None = Field(default=None, description="Updated model override")
    tool_groups: list[str] | None = Field(default=None, description="Updated tool group whitelist")
    skills: list[str] | None = Field(default=None, description="Updated skill whitelist (None=all, []=none)")
    soul: str | None = Field(default=None, description="Updated SOUL.md content")


def _validate_agent_name(name: str) -> None:
    """验证智能体名称是否符合允许的模式。

    仅允许字母、数字和连字符，防止路径遍历等安全风险。

    Args:
        name: 待验证的智能体名称。

    Raises:
        HTTPException: 状态码 422，当名称不符合规范时抛出。
    """
    if not AGENT_NAME_PATTERN.match(name):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid agent name '{name}'. Must match ^[A-Za-z0-9-]+$ (letters, digits, and hyphens only).",
        )


def _normalize_agent_name(name: str) -> str:
    """将智能体名称统一转为小写，用于文件系统存储。

    Args:
        name: 原始智能体名称。

    Returns:
        小写化后的名称字符串。
    """
    return name.lower()


def _require_agents_api_enabled() -> None:
    """拒绝访问，除非自定义智能体管理 API 已显式启用。

    Raises:
        HTTPException: 状态码 403，当 API 未启用时抛出。
    """
    if not get_agents_api_config().enabled:
        raise HTTPException(
            status_code=403,
            detail=("Custom-agent management API is disabled. Set agents_api.enabled=true to expose agent and user-profile routes over HTTP."),
        )


def _agent_config_to_response(agent_cfg: AgentConfig, include_soul: bool = False, *, user_id: str | None = None) -> AgentResponse:
    """将内部 AgentConfig 对象转换为 API 响应模型。

    Args:
        agent_cfg: 内部智能体配置对象。
        include_soul: 是否在响应中包含 SOUL.md 内容。
        user_id: 用户标识，用于定位用户级别的 SOUL.md 文件。

    Returns:
        转换后的 AgentResponse 对象。
    """
    soul: str | None = None
    if include_soul:
        soul = load_agent_soul(agent_cfg.name, user_id=user_id) or ""

    return AgentResponse(
        name=agent_cfg.name,
        description=agent_cfg.description,
        model=agent_cfg.model,
        tool_groups=agent_cfg.tool_groups,
        skills=agent_cfg.skills,
        soul=soul,
    )


@router.get(
    "/agents",
    response_model=AgentsListResponse,
    summary="List Custom Agents",
    description="List all custom agents available in the agents directory, including their soul content.",
)
async def list_agents() -> AgentsListResponse:
    """列出所有自定义智能体。

    返回当前用户可见的所有自定义智能体，包括元数据和 SOUL.md 内容。

    Returns:
        包含所有自定义智能体信息的列表响应。

    Raises:
        HTTPException: 状态码 500，当内部加载失败时抛出。
    """
    _require_agents_api_enabled()

    user_id = get_effective_user_id()
    try:
        agents = list_custom_agents(user_id=user_id)
        return AgentsListResponse(agents=[_agent_config_to_response(a, include_soul=True, user_id=user_id) for a in agents])
    except Exception as e:
        logger.error(f"Failed to list agents: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list agents: {str(e)}")


@router.get(
    "/agents/check",
    summary="Check Agent Name",
    description="Validate an agent name and check if it is available (case-insensitive).",
)
async def check_agent_name(name: str) -> dict:
    """检查智能体名称是否合法且未被占用（大小写不敏感）。

    Args:
        name: 待检查的智能体名称。

    Returns:
        包含 available（是否可用）和 name（标准化后的小写名称）的字典。

    Raises:
        HTTPException: 状态码 422，当名称不符合规范时抛出。
    """
    _require_agents_api_enabled()
    _validate_agent_name(name)
    normalized = _normalize_agent_name(name)
    user_id = get_effective_user_id()
    paths = get_paths()
    # 同时检查用户级目录和遗留共享目录：选择与未迁移的遗留智能体重名的名称
    # 会在迁移执行后遮蔽遗留条目，因此提前阻止
    available = not paths.user_agent_dir(user_id, normalized).exists() and not paths.agent_dir(normalized).exists()
    return {"available": available, "name": normalized}


@router.get(
    "/agents/{name}",
    response_model=AgentResponse,
    summary="Get Custom Agent",
    description="Retrieve details and SOUL.md content for a specific custom agent.",
)
async def get_agent(name: str) -> AgentResponse:
    """根据名称获取指定自定义智能体的详细信息。

    Args:
        name: 智能体名称。

    Returns:
        包含智能体详细信息和 SOUL.md 内容的响应。

    Raises:
        HTTPException: 状态码 404，当智能体不存在时抛出。
    """
    _require_agents_api_enabled()
    _validate_agent_name(name)
    name = _normalize_agent_name(name)
    user_id = get_effective_user_id()

    try:
        agent_cfg = load_agent_config(name, user_id=user_id)
        return _agent_config_to_response(agent_cfg, include_soul=True, user_id=user_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    except Exception as e:
        logger.error(f"Failed to get agent '{name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get agent: {str(e)}")


@router.post(
    "/agents",
    response_model=AgentResponse,
    status_code=201,
    summary="Create Custom Agent",
    description="Create a new custom agent with its config and SOUL.md.",
)
async def create_agent_endpoint(request: AgentCreateRequest) -> AgentResponse:
    """创建一个新的自定义智能体。

    在文件系统上创建对应的目录结构，写入 config.yaml 和 SOUL.md 文件。
    如果创建过程中发生错误，会自动清理已创建的目录。

    Args:
        request: 智能体创建请求体。

    Returns:
        创建成功的智能体详细信息。

    Raises:
        HTTPException: 状态码 409（名称已存在）或 422（名称不合法）。
    """
    _require_agents_api_enabled()
    _validate_agent_name(request.name)
    normalized_name = _normalize_agent_name(request.name)
    user_id = get_effective_user_id()
    paths = get_paths()

    agent_dir = paths.user_agent_dir(user_id, normalized_name)
    legacy_dir = paths.agent_dir(normalized_name)

    # 检查用户级目录和遗留共享目录是否已存在同名智能体
    if agent_dir.exists() or legacy_dir.exists():
        raise HTTPException(status_code=409, detail=f"Agent '{normalized_name}' already exists")

    try:
        agent_dir.mkdir(parents=True, exist_ok=True)

        # 构建 config.yaml 数据，仅写入非空字段
        config_data: dict = {"name": normalized_name}
        if request.description:
            config_data["description"] = request.description
        if request.model is not None:
            config_data["model"] = request.model
        if request.tool_groups is not None:
            config_data["tool_groups"] = request.tool_groups
        if request.skills is not None:
            config_data["skills"] = request.skills

        config_file = agent_dir / "config.yaml"
        with open(config_file, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True)

        # 写入 SOUL.md 人设文件
        soul_file = agent_dir / "SOUL.md"
        soul_file.write_text(request.soul, encoding="utf-8")

        logger.info(f"Created agent '{normalized_name}' at {agent_dir}")

        agent_cfg = load_agent_config(normalized_name, user_id=user_id)
        return _agent_config_to_response(agent_cfg, include_soul=True, user_id=user_id)

    except HTTPException:
        raise
    except Exception as e:
        # 创建失败时清理已生成的目录，避免残留脏数据
        if agent_dir.exists():
            shutil.rmtree(agent_dir)
        logger.error(f"Failed to create agent '{request.name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create agent: {str(e)}")


@router.put(
    "/agents/{name}",
    response_model=AgentResponse,
    summary="Update Custom Agent",
    description="Update an existing custom agent's config and/or SOUL.md.",
)
async def update_agent(name: str, request: AgentUpdateRequest) -> AgentResponse:
    """更新已存在的自定义智能体。

    支持部分更新：仅传入需要修改的字段即可。
    对于遗留共享布局中的智能体，拒绝直接更新，需先执行迁移脚本。

    Args:
        name: 智能体名称。
        request: 更新请求体，所有字段均为可选。

    Returns:
        更新后的智能体详细信息。

    Raises:
        HTTPException: 状态码 404（智能体不存在）或 409（遗留布局冲突）。
    """
    _require_agents_api_enabled()
    _validate_agent_name(name)
    name = _normalize_agent_name(name)
    user_id = get_effective_user_id()

    try:
        agent_cfg = load_agent_config(name, user_id=user_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

    paths = get_paths()
    agent_dir = paths.user_agent_dir(user_id, name)
    # 遗留共享布局的智能体不支持直接更新，需要先迁移到用户级目录
    if not agent_dir.exists() and paths.agent_dir(name).exists():
        raise HTTPException(
            status_code=409,
            detail=(f"Agent '{name}' only exists in the legacy shared layout and is not scoped to a user. Run scripts/migrate_user_isolation.py to move legacy agents into the per-user layout before updating."),
        )

    try:
        # 使用 model_fields_set 区分"字段省略"与"显式设为 null"。
        # 这对于 skills 字段尤为关键：None 表示"继承所有技能"（而非"不修改"）。
        fields_set = request.model_fields_set
        config_changed = bool(fields_set & {"description", "model", "tool_groups", "skills"})

        if config_changed:
            updated: dict = {
                "name": agent_cfg.name,
                "description": request.description if "description" in fields_set else agent_cfg.description,
            }
            new_model = request.model if "model" in fields_set else agent_cfg.model
            if new_model is not None:
                updated["model"] = new_model

            new_tool_groups = request.tool_groups if "tool_groups" in fields_set else agent_cfg.tool_groups
            if new_tool_groups is not None:
                updated["tool_groups"] = new_tool_groups

            # skills 字段的三种语义：None=继承所有，[]=无技能，["a","b"]=白名单
            if "skills" in fields_set:
                new_skills = request.skills
            else:
                new_skills = agent_cfg.skills
            if new_skills is not None:
                updated["skills"] = new_skills

            config_file = agent_dir / "config.yaml"
            with open(config_file, "w", encoding="utf-8") as f:
                yaml.dump(updated, f, default_flow_style=False, allow_unicode=True)

        # 更新 SOUL.md（仅在显式提供时）
        if request.soul is not None:
            soul_path = agent_dir / "SOUL.md"
            soul_path.write_text(request.soul, encoding="utf-8")

        logger.info(f"Updated agent '{name}'")

        refreshed_cfg = load_agent_config(name, user_id=user_id)
        return _agent_config_to_response(refreshed_cfg, include_soul=True, user_id=user_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update agent '{name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update agent: {str(e)}")


class UserProfileResponse(BaseModel):
    """用户全局配置文件（USER.md）的响应模型。

    Attributes:
        content: USER.md 文件内容，若尚未创建则为 None。
    """

    content: str | None = Field(default=None, description="USER.md content, or null if not yet created")


class UserProfileUpdateRequest(BaseModel):
    """更新用户全局配置文件的请求体。

    Attributes:
        content: USER.md 文件内容，描述用户背景和偏好。
    """

    content: str = Field(default="", description="USER.md content — describes the user's background and preferences")


@router.get(
    "/user-profile",
    response_model=UserProfileResponse,
    summary="Get User Profile",
    description="Read the global USER.md file that is injected into all custom agents.",
)
async def get_user_profile() -> UserProfileResponse:
    """读取当前用户的 USER.md 内容。

    USER.md 文件会被注入到所有自定义智能体的上下文中。

    Returns:
        UserProfileResponse，若 USER.md 不存在则 content 为 None。

    Raises:
        HTTPException: 状态码 500，当读取失败时抛出。
    """
    _require_agents_api_enabled()

    try:
        user_md_path = get_paths().user_md_file
        if not user_md_path.exists():
            return UserProfileResponse(content=None)
        raw = user_md_path.read_text(encoding="utf-8").strip()
        return UserProfileResponse(content=raw or None)
    except Exception as e:
        logger.error(f"Failed to read user profile: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to read user profile: {str(e)}")


@router.put(
    "/user-profile",
    response_model=UserProfileResponse,
    summary="Update User Profile",
    description="Write the global USER.md file that is injected into all custom agents.",
)
async def update_user_profile(request: UserProfileUpdateRequest) -> UserProfileResponse:
    """创建或覆盖全局 USER.md 文件。

    Args:
        request: 包含新 USER.md 内容的更新请求。

    Returns:
        UserProfileResponse，包含保存后的内容。

    Raises:
        HTTPException: 状态码 500，当写入失败时抛出。
    """
    _require_agents_api_enabled()

    try:
        paths = get_paths()
        paths.base_dir.mkdir(parents=True, exist_ok=True)
        paths.user_md_file.write_text(request.content, encoding="utf-8")
        logger.info(f"Updated USER.md at {paths.user_md_file}")
        return UserProfileResponse(content=request.content or None)
    except Exception as e:
        logger.error(f"Failed to update user profile: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update user profile: {str(e)}")


@router.delete(
    "/agents/{name}",
    status_code=204,
    summary="Delete Custom Agent",
    description="Delete a custom agent and all its files (config, SOUL.md, memory).",
)
async def delete_agent(name: str) -> None:
    """删除指定的自定义智能体。

    删除智能体目录及其下的所有文件（config.yaml、SOUL.md、记忆数据等）。
    对于遗留共享布局中的智能体，拒绝直接删除，需先执行迁移脚本。

    Args:
        name: 智能体名称。

    Raises:
        HTTPException: 状态码 404（智能体不存在）或 409（遗留布局冲突）。
    """
    _require_agents_api_enabled()
    _validate_agent_name(name)
    name = _normalize_agent_name(name)
    user_id = get_effective_user_id()
    paths = get_paths()
    agent_dir = paths.user_agent_dir(user_id, name)

    if not agent_dir.exists():
        # 检查是否为遗留共享布局中的智能体
        if paths.agent_dir(name).exists():
            raise HTTPException(
                status_code=409,
                detail=(f"Agent '{name}' only exists in the legacy shared layout and is not scoped to a user. Run scripts/migrate_user_isolation.py to move legacy agents into the per-user layout before deleting."),
            )
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

    try:
        shutil.rmtree(agent_dir)
        logger.info(f"Deleted agent '{name}' from {agent_dir}")
    except Exception as e:
        logger.error(f"Failed to delete agent '{name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete agent: {str(e)}")
