"""技能（Skill）的 CRUD 与安装管理路由。

本模块实现了 DeerFlow 技能系统的完整管理 API。技能是以 Markdown
格式定义的可复用知识模块，AI 智能体可以在运行时调用这些技能来
扩展自身能力。

技能分类：
- public: 系统预置的公共技能，随应用一起分发
- custom: 用户自定义的技能，通过手动创建或安装 .skill 归档获得

核心功能：
- 列出所有技能（公共 + 自定义）
- 获取/更新单个技能的启用状态
- 从 .skill 归档文件安装新技能
- 自定义技能的内容读取、编辑、删除
- 自定义技能的编辑历史查询与回滚

安全机制：
- 自定义技能内容经过安全扫描（security_scanner）后才能写入
- 技能名称去除换行符以防止 CRLF 注入
- 归档文件大小和内容均有安全检查

路由前缀：/api
标签：skills
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.gateway.deps import get_config
from app.gateway.path_utils import resolve_thread_virtual_path
from deerflow.agents.lead_agent.prompt import refresh_skills_system_prompt_cache_async
from deerflow.config.app_config import AppConfig
from deerflow.config.extensions_config import ExtensionsConfig, SkillStateConfig, get_extensions_config, reload_extensions_config
from deerflow.skills import Skill
from deerflow.skills.installer import SkillAlreadyExistsError
from deerflow.skills.security_scanner import scan_skill_content
from deerflow.skills.storage import get_or_new_skill_storage
from deerflow.skills.types import SKILL_MD_FILE, SkillCategory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["skills"])


class SkillResponse(BaseModel):
    """技能信息响应模型。

    Attributes:
        name: 技能名称。
        description: 技能功能描述。
        license: 许可证信息。
        category: 技能类别（public 或 custom）。
        enabled: 是否启用。
    """

    name: str = Field(..., description="Name of the skill")
    description: str = Field(..., description="Description of what the skill does")
    license: str | None = Field(None, description="License information")
    category: SkillCategory = Field(..., description="Category of the skill (public or custom)")
    enabled: bool = Field(default=True, description="Whether this skill is enabled")


class SkillsListResponse(BaseModel):
    """技能列表响应模型。"""

    skills: list[SkillResponse]


class SkillUpdateRequest(BaseModel):
    """技能状态更新请求模型。

    Attributes:
        enabled: 是否启用该技能。
    """

    enabled: bool = Field(..., description="Whether to enable or disable the skill")


class SkillInstallRequest(BaseModel):
    """从 .skill 文件安装技能的请求模型。

    Attributes:
        thread_id: .skill 文件所在的线程 ID。
        path: .skill 文件的虚拟路径（如 mnt/user-data/outputs/my-skill.skill）。
    """

    thread_id: str = Field(..., description="The thread ID where the .skill file is located")
    path: str = Field(..., description="Virtual path to the .skill file (e.g., mnt/user-data/outputs/my-skill.skill)")


class SkillInstallResponse(BaseModel):
    """技能安装结果响应模型。

    Attributes:
        success: 安装是否成功。
        skill_name: 已安装的技能名称。
        message: 安装结果描述。
    """

    success: bool = Field(..., description="Whether the installation was successful")
    skill_name: str = Field(..., description="Name of the installed skill")
    message: str = Field(..., description="Installation result message")


class CustomSkillContentResponse(SkillResponse):
    """自定义技能内容响应模型（包含 SKILL.md 原始内容）。

    Attributes:
        content: SKILL.md 文件的原始 Markdown 内容。
    """

    content: str = Field(..., description="Raw SKILL.md content")


class CustomSkillUpdateRequest(BaseModel):
    """自定义技能内容更新请求模型。

    Attributes:
        content: 替换后的 SKILL.md 内容。
    """

    content: str = Field(..., description="Replacement SKILL.md content")


class CustomSkillHistoryResponse(BaseModel):
    """自定义技能编辑历史响应模型。"""

    history: list[dict]


class SkillRollbackRequest(BaseModel):
    """技能回滚请求模型。

    Attributes:
        history_index: 要恢复到的历史条目索引，默认为最近一次变更。
    """

    history_index: int = Field(default=-1, description="History entry index to restore from, defaulting to the latest change.")


def _skill_to_response(skill: Skill) -> SkillResponse:
    """将内部 Skill 对象转换为 API 响应模型。

    Args:
        skill: 内部技能对象。

    Returns:
        转换后的 SkillResponse。
    """
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        license=skill.license,
        category=skill.category,
        enabled=skill.enabled,
    )


@router.get(
    "/skills",
    response_model=SkillsListResponse,
    summary="List All Skills",
    description="Retrieve a list of all available skills from both public and custom directories.",
)
async def list_skills(config: AppConfig = Depends(get_config)) -> SkillsListResponse:
    """列出所有可用技能（公共 + 自定义）。

    Args:
        config: 应用配置对象（通过依赖注入获取）。

    Returns:
        SkillsListResponse，包含所有技能信息。

    Raises:
        HTTPException: 状态码 500，当加载失败时抛出。
    """
    try:
        skills = get_or_new_skill_storage(app_config=config).load_skills(enabled_only=False)
        return SkillsListResponse(skills=[_skill_to_response(skill) for skill in skills])
    except Exception as e:
        logger.error(f"Failed to load skills: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to load skills: {str(e)}")


@router.post(
    "/skills/install",
    response_model=SkillInstallResponse,
    summary="Install Skill",
    description="Install a skill from a .skill file (ZIP archive) located in the thread's user-data directory.",
)
async def install_skill(request: SkillInstallRequest, config: AppConfig = Depends(get_config)) -> SkillInstallResponse:
    """从 .skill 归档文件安装新技能。

    .skill 文件为 ZIP 格式归档，包含 SKILL.md 和可选的附加资源。
    安装成功后自动刷新系统提示缓存。

    Args:
        request: 安装请求体。
        config: 应用配置对象。

    Returns:
        SkillInstallResponse，包含安装结果。

    Raises:
        HTTPException: 状态码 404（文件不存在）、409（技能已存在）、400（格式错误）。
    """
    try:
        skill_file_path = resolve_thread_virtual_path(request.thread_id, request.path)
        result = await get_or_new_skill_storage(app_config=config).ainstall_skill_from_archive(skill_file_path)
        # 安装成功后刷新系统提示缓存，使新技能立即生效
        await refresh_skills_system_prompt_cache_async()
        return SkillInstallResponse(**result)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except SkillAlreadyExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to install skill: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to install skill: {str(e)}")


@router.get("/skills/custom", response_model=SkillsListResponse, summary="List Custom Skills")
async def list_custom_skills(config: AppConfig = Depends(get_config)) -> SkillsListResponse:
    """列出所有自定义技能。

    仅返回 category 为 custom 的技能。

    Args:
        config: 应用配置对象。

    Returns:
        SkillsListResponse，仅包含自定义技能。

    Raises:
        HTTPException: 状态码 500，当加载失败时抛出。
    """
    try:
        skills = [skill for skill in get_or_new_skill_storage(app_config=config).load_skills(enabled_only=False) if skill.category == SkillCategory.CUSTOM]
        return SkillsListResponse(skills=[_skill_to_response(skill) for skill in skills])
    except Exception as e:
        logger.error("Failed to list custom skills: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list custom skills: {str(e)}")


@router.get("/skills/custom/{skill_name}", response_model=CustomSkillContentResponse, summary="Get Custom Skill Content")
async def get_custom_skill(skill_name: str, config: AppConfig = Depends(get_config)) -> CustomSkillContentResponse:
    """获取自定义技能的完整内容（包括 SKILL.md 原文）。

    Args:
        skill_name: 技能名称。
        config: 应用配置对象。

    Returns:
        CustomSkillContentResponse，包含技能元数据和 SKILL.md 内容。

    Raises:
        HTTPException: 状态码 404（技能不存在）或 500（读取失败）。
    """
    try:
        # 去除换行符以防止 CRLF 注入
        skill_name = skill_name.replace("\r\n", "").replace("\n", "")
        skills = get_or_new_skill_storage(app_config=config).load_skills(enabled_only=False)
        skill = next((s for s in skills if s.name == skill_name and s.category == SkillCategory.CUSTOM), None)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"Custom skill '{skill_name}' not found")
        return CustomSkillContentResponse(**_skill_to_response(skill).model_dump(), content=get_or_new_skill_storage(app_config=config).read_custom_skill(skill_name))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get custom skill %s: %s", skill_name, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get custom skill: {str(e)}")


@router.put("/skills/custom/{skill_name}", response_model=CustomSkillContentResponse, summary="Edit Custom Skill")
async def update_custom_skill(skill_name: str, request: CustomSkillUpdateRequest, config: AppConfig = Depends(get_config)) -> CustomSkillContentResponse:
    """编辑自定义技能的 SKILL.md 内容。

    编辑前会执行安全扫描，阻止包含危险内容的修改。
    编辑操作记录到历史中，支持后续回滚。

    Args:
        skill_name: 技能名称。
        request: 内容更新请求体。
        config: 应用配置对象。

    Returns:
        CustomSkillContentResponse，更新后的技能内容和元数据。

    Raises:
        HTTPException: 状态码 400（安全扫描阻止）、404（技能不存在）。
    """
    try:
        skill_name = skill_name.replace("\r\n", "").replace("\n", "")
        storage = get_or_new_skill_storage(app_config=config)
        storage.ensure_custom_skill_is_editable(skill_name)
        storage.validate_skill_markdown_content(skill_name, request.content)
        # 对新内容执行安全扫描，仅扫描非可执行内容
        scan = await scan_skill_content(request.content, executable=False, location=f"{skill_name}/{SKILL_MD_FILE}", app_config=config)
        if scan.decision == "block":
            raise HTTPException(status_code=400, detail=f"Security scan blocked the edit: {scan.reason}")
        # 保存编辑前的内容用于历史记录
        prev_content = storage.read_custom_skill(skill_name)
        storage.write_custom_skill(skill_name, SKILL_MD_FILE, request.content)
        # 记录编辑历史
        storage.append_history(
            skill_name,
            {
                "action": "human_edit",
                "author": "human",
                "thread_id": None,
                "file_path": SKILL_MD_FILE,
                "prev_content": prev_content,
                "new_content": request.content,
                "scanner": {"decision": scan.decision, "reason": scan.reason},
            },
        )
        await refresh_skills_system_prompt_cache_async()
        return await get_custom_skill(skill_name, config)
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Failed to update custom skill %s: %s", skill_name, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update custom skill: {str(e)}")


@router.delete("/skills/custom/{skill_name}", summary="Delete Custom Skill")
async def delete_custom_skill(skill_name: str, config: AppConfig = Depends(get_config)) -> dict[str, bool]:
    """删除自定义技能。

    删除操作记录到历史中。

    Args:
        skill_name: 技能名称。
        config: 应用配置对象。

    Returns:
        包含 success=True 的字典。

    Raises:
        HTTPException: 状态码 404（技能不存在）或 400（验证失败）。
    """
    try:
        skill_name = skill_name.replace("\r\n", "").replace("\n", "")
        storage = get_or_new_skill_storage(app_config=config)
        storage.delete_custom_skill(
            skill_name,
            history_meta={
                "action": "human_delete",
                "author": "human",
                "thread_id": None,
                "file_path": SKILL_MD_FILE,
                "prev_content": None,
                "new_content": None,
                "scanner": {"decision": "allow", "reason": "Deletion requested."},
            },
        )
        await refresh_skills_system_prompt_cache_async()
        return {"success": True}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Failed to delete custom skill %s: %s", skill_name, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete custom skill: {str(e)}")


@router.get("/skills/custom/{skill_name}/history", response_model=CustomSkillHistoryResponse, summary="Get Custom Skill History")
async def get_custom_skill_history(skill_name: str, config: AppConfig = Depends(get_config)) -> CustomSkillHistoryResponse:
    """获取自定义技能的编辑历史。

    Args:
        skill_name: 技能名称。
        config: 应用配置对象。

    Returns:
        CustomSkillHistoryResponse，包含编辑历史列表。

    Raises:
        HTTPException: 状态码 404（技能不存在）或 500（读取失败）。
    """
    try:
        skill_name = skill_name.replace("\r\n", "").replace("\n", "")
        storage = get_or_new_skill_storage(app_config=config)
        if not storage.custom_skill_exists(skill_name) and not storage.get_skill_history_file(skill_name).exists():
            raise HTTPException(status_code=404, detail=f"Custom skill '{skill_name}' not found")
        return CustomSkillHistoryResponse(history=storage.read_history(skill_name))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to read history for %s: %s", skill_name, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to read history: {str(e)}")


@router.post("/skills/custom/{skill_name}/rollback", response_model=CustomSkillContentResponse, summary="Rollback Custom Skill")
async def rollback_custom_skill(skill_name: str, request: SkillRollbackRequest, config: AppConfig = Depends(get_config)) -> CustomSkillContentResponse:
    """将自定义技能回滚到历史中的某个版本。

    从编辑历史中选取目标版本的 prev_content 作为回滚目标。
    回滚前执行安全扫描，扫描结果（无论是否阻止）都记录到历史中。

    Args:
        skill_name: 技能名称。
        request: 回滚请求体（包含历史索引）。
        config: 应用配置对象。

    Returns:
        CustomSkillContentResponse，回滚后的技能内容。

    Raises:
        HTTPException: 状态码 400（安全扫描阻止/无历史/索引越界）、404（技能不存在）。
    """
    try:
        storage = get_or_new_skill_storage(app_config=config)
        if not storage.custom_skill_exists(skill_name) and not storage.get_skill_history_file(skill_name).exists():
            raise HTTPException(status_code=404, detail=f"Custom skill '{skill_name}' not found")
        history = storage.read_history(skill_name)
        if not history:
            raise HTTPException(status_code=400, detail=f"Custom skill '{skill_name}' has no history")
        record = history[request.history_index]
        # 回滚目标为历史记录中的 prev_content（即变更前的内容）
        target_content = record.get("prev_content")
        if target_content is None:
            raise HTTPException(status_code=400, detail="Selected history entry has no previous content to roll back to")
        storage.validate_skill_markdown_content(skill_name, target_content)
        # 对回滚目标内容执行安全扫描
        scan = await scan_skill_content(target_content, executable=False, location=f"{skill_name}/{SKILL_MD_FILE}", app_config=config)
        skill_file = storage.get_custom_skill_file(skill_name)
        current_content = skill_file.read_text(encoding="utf-8") if skill_file.exists() else None
        history_entry = {
            "action": "rollback",
            "author": "human",
            "thread_id": None,
            "file_path": SKILL_MD_FILE,
            "prev_content": current_content,
            "new_content": target_content,
            "rollback_from_ts": record.get("ts"),
            "scanner": {"decision": scan.decision, "reason": scan.reason},
        }
        # 即使安全扫描阻止回滚，也将尝试记录到历史中
        if scan.decision == "block":
            storage.append_history(skill_name, history_entry)
            raise HTTPException(status_code=400, detail=f"Rollback blocked by security scanner: {scan.reason}")
        storage.write_custom_skill(skill_name, SKILL_MD_FILE, target_content)
        storage.append_history(skill_name, history_entry)
        await refresh_skills_system_prompt_cache_async()
        return await get_custom_skill(skill_name, config)
    except HTTPException:
        raise
    except IndexError:
        raise HTTPException(status_code=400, detail="history_index is out of range")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Failed to roll back custom skill %s: %s", skill_name, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to roll back custom skill: {str(e)}")


@router.get(
    "/skills/{skill_name}",
    response_model=SkillResponse,
    summary="Get Skill Details",
    description="Retrieve detailed information about a specific skill by its name.",
)
async def get_skill(skill_name: str, config: AppConfig = Depends(get_config)) -> SkillResponse:
    """按名称获取单个技能的详细信息。

    Args:
        skill_name: 技能名称。
        config: 应用配置对象。

    Returns:
        SkillResponse，技能详细信息。

    Raises:
        HTTPException: 状态码 404（技能不存在）。
    """
    try:
        skill_name = skill_name.replace("\r\n", "").replace("\n", "")
        skills = get_or_new_skill_storage(app_config=config).load_skills(enabled_only=False)
        skill = next((s for s in skills if s.name == skill_name), None)

        if skill is None:
            raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")

        return _skill_to_response(skill)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get skill {skill_name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get skill: {str(e)}")


@router.put(
    "/skills/{skill_name}",
    response_model=SkillResponse,
    summary="Update Skill",
    description="Update a skill's enabled status by modifying the extensions_config.json file.",
)
async def update_skill(skill_name: str, request: SkillUpdateRequest, config: AppConfig = Depends(get_config)) -> SkillResponse:
    """更新技能的启用/禁用状态。

    通过修改 extensions_config.json 文件持久化启用状态变更，
    同时保留 MCP 服务器配置不受影响。更新后自动重载配置缓存
    并刷新系统提示。

    Args:
        skill_name: 技能名称。
        request: 状态更新请求体。
        config: 应用配置对象。

    Returns:
        SkillResponse，更新后的技能信息。

    Raises:
        HTTPException: 状态码 404（技能不存在）或 500（更新失败）。
    """
    try:
        skill_name = skill_name.replace("\r\n", "").replace("\n", "")
        skills = get_or_new_skill_storage(app_config=config).load_skills(enabled_only=False)
        skill = next((s for s in skills if s.name == skill_name), None)

        if skill is None:
            raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")

        # 定位或创建配置文件
        config_path = ExtensionsConfig.resolve_config_path()
        if config_path is None:
            config_path = Path.cwd().parent / "extensions_config.json"
            logger.info(f"No existing extensions config found. Creating new config at: {config_path}")

        # 更新技能启用状态，同时保留 MCP 配置
        extensions_config = get_extensions_config()
        extensions_config.skills[skill_name] = SkillStateConfig(enabled=request.enabled)

        config_data = {
            "mcpServers": {name: server.model_dump() for name, server in extensions_config.mcp_servers.items()},
            "skills": {name: {"enabled": skill_config.enabled} for name, skill_config in extensions_config.skills.items()},
        }

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)

        logger.info(f"Skills configuration updated and saved to: {config_path}")
        # 重载配置缓存
        reload_extensions_config()
        # 刷新系统提示缓存使变更立即生效
        await refresh_skills_system_prompt_cache_async()

        # 重新加载验证更新是否成功
        skills = get_or_new_skill_storage(app_config=config).load_skills(enabled_only=False)
        updated_skill = next((s for s in skills if s.name == skill_name), None)

        if updated_skill is None:
            raise HTTPException(status_code=500, detail=f"Failed to reload skill '{skill_name}' after update")

        logger.info(f"Skill '{skill_name}' enabled status updated to {request.enabled}")
        return _skill_to_response(updated_skill)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update skill {skill_name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update skill: {str(e)}")
