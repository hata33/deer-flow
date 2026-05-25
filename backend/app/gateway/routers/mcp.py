"""MCP（Model Context Protocol）服务器配置管理路由。

本模块提供 MCP 服务器配置的读取和更新接口。MCP 是一种标准协议，
允许 AI 智能体通过统一的工具接口与外部服务（如 GitHub、数据库、
搜索引擎等）进行交互。

核心功能：
- 读取当前 MCP 服务器配置（GET /api/mcp/config）
- 更新 MCP 服务器配置并持久化到文件（PUT /api/mcp/config）

配置结构：
- 每个 MCP 服务器由名称（键）和配置（值）组成
- 支持三种传输类型：stdio（本地进程）、sse（Server-Sent Events）、http
- stdio 类型需要 command 和 args 参数
- sse/http 类型需要 url 和可选的 headers
- 支持 OAuth 令牌注入配置

持久化：
- 配置存储在 extensions_config.json 文件中
- 更新时会同时保存 MCP 和 Skills 配置（避免覆盖 skills 部分）
- 更新后自动重载内存缓存

注意：LangGraph Server（独立进程）会通过 mtime 检测配置文件变更
并自动重新初始化 MCP 工具，因此本模块无需主动触发工具重置。

路由前缀：/api
标签：mcp
"""

import json
import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from deerflow.config.extensions_config import ExtensionsConfig, get_extensions_config, reload_extensions_config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["mcp"])


class McpOAuthConfigResponse(BaseModel):
    """MCP 服务器的 OAuth 配置模型。

    Attributes:
        enabled: 是否启用 OAuth 令牌注入。
        token_url: OAuth 令牌端点 URL。
        grant_type: OAuth 授权类型（client_credentials 或 refresh_token）。
        client_id: OAuth 客户端 ID。
        client_secret: OAuth 客户端密钥。
        refresh_token: OAuth 刷新令牌。
        scope: OAuth 作用域。
        audience: OAuth 受众。
        token_field: 令牌响应中包含访问令牌的字段名。
        token_type_field: 令牌响应中包含令牌类型的字段名。
        expires_in_field: 令牌响应中包含过期时间的字段名。
        default_token_type: 响应省略 token_type 时使用的默认值。
        refresh_skew_seconds: 在过期前多少秒刷新令牌。
        extra_token_params: 发送到令牌端点的额外表单参数。
    """

    enabled: bool = Field(default=True, description="Whether OAuth token injection is enabled")
    token_url: str = Field(default="", description="OAuth token endpoint URL")
    grant_type: Literal["client_credentials", "refresh_token"] = Field(default="client_credentials", description="OAuth grant type")
    client_id: str | None = Field(default=None, description="OAuth client ID")
    client_secret: str | None = Field(default=None, description="OAuth client secret")
    refresh_token: str | None = Field(default=None, description="OAuth refresh token")
    scope: str | None = Field(default=None, description="OAuth scope")
    audience: str | None = Field(default=None, description="OAuth audience")
    token_field: str = Field(default="access_token", description="Token response field containing access token")
    token_type_field: str = Field(default="token_type", description="Token response field containing token type")
    expires_in_field: str = Field(default="expires_in", description="Token response field containing expires-in seconds")
    default_token_type: str = Field(default="Bearer", description="Default token type when response omits token_type")
    refresh_skew_seconds: int = Field(default=60, description="Refresh this many seconds before expiry")
    extra_token_params: dict[str, str] = Field(default_factory=dict, description="Additional form params sent to token endpoint")


class McpServerConfigResponse(BaseModel):
    """单个 MCP 服务器的配置响应模型。

    Attributes:
        enabled: 是否启用此 MCP 服务器。
        type: 传输类型（stdio、sse 或 http）。
        command: 启动 MCP 服务器的命令（stdio 类型）。
        args: 传递给命令的参数列表（stdio 类型）。
        env: MCP 服务器的环境变量。
        url: MCP 服务器 URL（sse/http 类型）。
        headers: 发送的 HTTP 头（sse/http 类型）。
        oauth: OAuth 配置（sse/http 类型可选）。
        description: 服务器功能的可读描述。
    """

    enabled: bool = Field(default=True, description="Whether this MCP server is enabled")
    type: str = Field(default="stdio", description="Transport type: 'stdio', 'sse', or 'http'")
    command: str | None = Field(default=None, description="Command to execute to start the MCP server (for stdio type)")
    args: list[str] = Field(default_factory=list, description="Arguments to pass to the command (for stdio type)")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables for the MCP server")
    url: str | None = Field(default=None, description="URL of the MCP server (for sse or http type)")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP headers to send (for sse or http type)")
    oauth: McpOAuthConfigResponse | None = Field(default=None, description="OAuth configuration for MCP HTTP/SSE servers")
    description: str = Field(default="", description="Human-readable description of what this MCP server provides")


class McpConfigResponse(BaseModel):
    """MCP 完整配置响应模型。

    Attributes:
        mcp_servers: MCP 服务器名称到配置的映射字典。
    """

    mcp_servers: dict[str, McpServerConfigResponse] = Field(
        default_factory=dict,
        description="Map of MCP server name to configuration",
    )


class McpConfigUpdateRequest(BaseModel):
    """MCP 配置更新请求模型。

    Attributes:
        mcp_servers: 新的 MCP 服务器配置映射。
    """

    mcp_servers: dict[str, McpServerConfigResponse] = Field(
        ...,
        description="Map of MCP server name to configuration",
    )


@router.get(
    "/mcp/config",
    response_model=McpConfigResponse,
    summary="Get MCP Configuration",
    description="Retrieve the current Model Context Protocol (MCP) server configurations.",
)
async def get_mcp_configuration() -> McpConfigResponse:
    """获取当前 MCP 服务器配置。

    返回所有已注册的 MCP 服务器配置信息，包括传输类型、
    启动参数、环境变量、OAuth 配置等。

    Returns:
        McpConfigResponse，包含所有 MCP 服务器配置。
    """
    config = get_extensions_config()

    return McpConfigResponse(mcp_servers={name: McpServerConfigResponse(**server.model_dump()) for name, server in config.mcp_servers.items()})


@router.put(
    "/mcp/config",
    response_model=McpConfigResponse,
    summary="Update MCP Configuration",
    description="Update Model Context Protocol (MCP) server configurations and save to file.",
)
async def update_mcp_configuration(request: McpConfigUpdateRequest) -> McpConfigResponse:
    """更新 MCP 服务器配置。

    执行以下步骤：
    1. 将新配置序列化为 JSON 并写入 extensions_config.json
    2. 保留现有的 skills 配置，避免被覆盖
    3. 重载内存中的配置缓存

    注意：LangGraph Server（独立进程）会通过 mtime 检测文件变更并
    自动重新初始化 MCP 工具，无需本端点主动触发。

    Args:
        request: 包含新 MCP 配置的请求体。

    Returns:
        更新后的 MCP 配置。

    Raises:
        HTTPException: 状态码 500，当配置文件写入失败时抛出。
    """
    try:
        # 获取当前配置文件路径（或确定保存位置）
        config_path = ExtensionsConfig.resolve_config_path()

        # 若无现有配置文件，在父目录（项目根目录）创建新文件
        if config_path is None:
            config_path = Path.cwd().parent / "extensions_config.json"
            logger.info(f"No existing extensions config found. Creating new config at: {config_path}")

        # 读取当前配置以保留 skills 部分
        current_config = get_extensions_config()

        # 将请求转换为字典格式用于 JSON 序列化
        config_data = {
            "mcpServers": {name: server.model_dump() for name, server in request.mcp_servers.items()},
            "skills": {name: {"enabled": skill.enabled} for name, skill in current_config.skills.items()},
        }

        # 写入配置文件
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)

        logger.info(f"MCP configuration updated and saved to: {config_path}")

        # 重载配置并更新全局缓存
        reloaded_config = reload_extensions_config()
        return McpConfigResponse(mcp_servers={name: McpServerConfigResponse(**server.model_dump()) for name, server in reloaded_config.mcp_servers.items()})

    except Exception as e:
        logger.error(f"Failed to update MCP configuration: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update MCP configuration: {str(e)}")
