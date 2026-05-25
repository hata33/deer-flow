"""OAuth 令牌管理 —— MCP HTTP/SSE 服务器的自动认证。

本模块为需要 OAuth 认证的 MCP 服务器提供令牌管理能力，
包括令牌获取、缓存、自动刷新和请求拦截注入。

核心问题:
  部分 MCP 服务器（如企业内部 API 网关、受保护的第三方服务）
  要求客户端在每次请求中携带 OAuth Bearer Token。
  手动管理令牌生命周期（获取、缓存、过期刷新）非常繁琐且容易出错。

解决方案:
  1. OAuthTokenManager:
     - 根据 extensions_config.json 中的 oauth 配置自动获取令牌
     - 内存缓存令牌，避免每次请求都重新获取
     - 自动检测令牌即将过期并刷新（通过 refresh_skew_seconds 提前刷新）
     - 使用 per-server asyncio.Lock 防止并发刷新

  2. build_oauth_tool_interceptor:
     - 构建 langchain-mcp-adapters 的工具拦截器
     - 在每次 MCP 工具调用前自动注入 Authorization 头
     - 令牌获取/刷新对上层代码完全透明

  3. get_initial_oauth_headers:
     - 在 MCP 客户端初始化时获取令牌
     - 用于 SSE/HTTP 连接建立时的首次认证

支持的 OAuth 授权类型:
  - client_credentials: 客户端凭证模式（服务器间通信）
  - refresh_token:      刷新令牌模式（长期访问）

配置示例 (extensions_config.json):
  {
    "mcpServers": {
      "my-api": {
        "type": "sse",
        "url": "https://api.example.com/mcp",
        "oauth": {
          "enabled": true,
          "token_url": "https://auth.example.com/oauth/token",
          "grant_type": "client_credentials",
          "client_id": "my-client-id",
          "client_secret": "my-client-secret",
          "scope": "read write"
        }
      }
    }
  }
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from deerflow.config.extensions_config import ExtensionsConfig, McpOAuthConfig

logger = logging.getLogger(__name__)


@dataclass
class _OAuthToken:
    """缓存的 OAuth 令牌数据。

    Attributes:
        access_token: 访问令牌字符串
        token_type:   令牌类型（通常为 "Bearer"）
        expires_at:   过期时间（UTC），用于判断是否需要刷新
    """

    access_token: str
    token_type: str
    expires_at: datetime


class OAuthTokenManager:
    """OAuth 令牌管理器 —— 获取、缓存、自动刷新。

    为每个配置了 OAuth 的 MCP 服务器管理独立的令牌生命周期。
    使用 per-server 的 asyncio.Lock 确保同一服务器的令牌获取/刷新
    不会并发执行（double-check locking 模式）。

    令牌刷新策略:
      在令牌实际过期前 refresh_skew_seconds 秒就触发刷新。
      这避免了"令牌刚获取就已过期"的边界情况（网络延迟、时钟偏差）。
      默认提前 60 秒刷新。
    """

    def __init__(self, oauth_by_server: dict[str, McpOAuthConfig]) -> None:
        # 每个 MCP 服务器对应的 OAuth 配置
        self._oauth_by_server = oauth_by_server
        # 令牌缓存：server_name → _OAuthToken
        self._tokens: dict[str, _OAuthToken] = {}
        # 每个 server 独立的锁，防止并发刷新同一 server 的令牌
        self._locks: dict[str, asyncio.Lock] = {name: asyncio.Lock() for name in oauth_by_server}

    @classmethod
    def from_extensions_config(cls, extensions_config: ExtensionsConfig) -> OAuthTokenManager:
        """从扩展配置构建 OAuthTokenManager。

        只收集配置了 oauth.enabled=true 的服务器。

        Args:
            extensions_config: 扩展配置对象

        Returns:
            配置好的 OAuthTokenManager 实例
        """
        oauth_by_server: dict[str, McpOAuthConfig] = {}
        for server_name, server_config in extensions_config.get_enabled_mcp_servers().items():
            if server_config.oauth and server_config.oauth.enabled:
                oauth_by_server[server_name] = server_config.oauth
        return cls(oauth_by_server)

    def has_oauth_servers(self) -> bool:
        """是否有配置了 OAuth 的服务器。"""
        return bool(self._oauth_by_server)

    def oauth_server_names(self) -> list[str]:
        """返回所有配置了 OAuth 的服务器名称列表。"""
        return list(self._oauth_by_server.keys())

    async def get_authorization_header(self, server_name: str) -> str | None:
        """获取指定服务器的 Authorization 头值。

        使用 double-check locking 模式：
          1. 先不加锁检查缓存
          2. 如果缓存有效直接返回（快速路径）
          3. 如果缓存无效，加锁后再次检查（可能其他协程已刷新）
          4. 确认需要刷新后执行令牌获取

        Args:
            server_name: MCP 服务器名称

        Returns:
            "Bearer xxx" 格式的认证头值，服务器未配置 OAuth 时返回 None
        """
        oauth = self._oauth_by_server.get(server_name)
        if not oauth:
            return None

        # 快速路径：缓存命中且未过期
        token = self._tokens.get(server_name)
        if token and not self._is_expiring(token, oauth):
            return f"{token.token_type} {token.access_token}"

        # 慢速路径：需要获取/刷新令牌
        lock = self._locks[server_name]
        async with lock:
            # double-check：其他协程可能已经在等待锁期间刷新了令牌
            token = self._tokens.get(server_name)
            if token and not self._is_expiring(token, oauth):
                return f"{token.token_type} {token.access_token}"

            # 确实需要刷新
            fresh = await self._fetch_token(oauth)
            self._tokens[server_name] = fresh
            logger.info(f"Refreshed OAuth access token for MCP server: {server_name}")
            return f"{fresh.token_type} {fresh.access_token}"

    @staticmethod
    def _is_expiring(token: _OAuthToken, oauth: McpOAuthConfig) -> bool:
        """检查令牌是否即将过期。

        在令牌实际过期前 refresh_skew_seconds 秒就认为需要刷新。
        提前刷新的目的是避免"令牌获取后立即过期"的竞态条件。

        Args:
            token: 缓存的令牌
            oauth: OAuth 配置（包含 refresh_skew_seconds）

        Returns:
            True 表示需要刷新
        """
        now = datetime.now(UTC)
        return token.expires_at <= now + timedelta(seconds=max(oauth.refresh_skew_seconds, 0))

    async def _fetch_token(self, oauth: McpOAuthConfig) -> _OAuthToken:
        """从 OAuth 令牌端点获取新的访问令牌。

        根据授权类型（grant_type）构建不同的请求参数：
          - client_credentials: 客户端凭证模式，使用 client_id + client_secret
          - refresh_token:      刷新令牌模式，使用 refresh_token + 可选的 client_id/secret

        令牌响应字段名可配置（token_field, token_type_field, expires_in_field），
        以适配不同 OAuth 提供商的非标准响应格式。

        Args:
            oauth: OAuth 配置

        Returns:
            获取到的令牌数据

        Raises:
            ValueError: 配置缺少必要字段（如 client_credentials 缺少 client_id）
            httpx.HTTPStatusError: 令牌端点返回错误状态码
        """
        import httpx  # pyright: ignore[reportMissingImports]

        # 构建令牌请求参数
        data: dict[str, str] = {
            "grant_type": oauth.grant_type,
            **oauth.extra_token_params,  # 额外的自定义参数
        }

        if oauth.scope:
            data["scope"] = oauth.scope
        if oauth.audience:
            data["audience"] = oauth.audience

        # 根据授权类型添加特定参数
        if oauth.grant_type == "client_credentials":
            if not oauth.client_id or not oauth.client_secret:
                raise ValueError("OAuth client_credentials requires client_id and client_secret")
            data["client_id"] = oauth.client_id
            data["client_secret"] = oauth.client_secret

        elif oauth.grant_type == "refresh_token":
            if not oauth.refresh_token:
                raise ValueError("OAuth refresh_token grant requires refresh_token")
            data["refresh_token"] = oauth.refresh_token
            # 某些提供商要求在刷新时也提供 client_id
            if oauth.client_id:
                data["client_id"] = oauth.client_id
            if oauth.client_secret:
                data["client_secret"] = oauth.client_secret

        else:
            raise ValueError(f"Unsupported OAuth grant type: {oauth.grant_type}")

        # 发送令牌请求
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(oauth.token_url, data=data)
            response.raise_for_status()
            payload = response.json()

        # 从响应中提取令牌信息
        # 字段名可配置，适配不同 OAuth 提供商的响应格式
        access_token = payload.get(oauth.token_field)
        if not access_token:
            raise ValueError(f"OAuth token response missing '{oauth.token_field}'")

        token_type = str(payload.get(oauth.token_type_field, oauth.default_token_type) or oauth.default_token_type)

        # 解析过期时间（秒），默认 3600 秒（1 小时）
        expires_in_raw = payload.get(oauth.expires_in_field, 3600)
        try:
            expires_in = int(expires_in_raw)
        except (TypeError, ValueError):
            expires_in = 3600

        # 计算绝对过期时间
        expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in, 1))
        return _OAuthToken(access_token=access_token, token_type=token_type, expires_at=expires_at)


def build_oauth_tool_interceptor(extensions_config: ExtensionsConfig) -> Any | None:
    """构建 OAuth 工具拦截器 —— 在每次 MCP 工具调用时自动注入认证头。

    返回一个符合 langchain-mcp-adapters 工具拦截器签名的异步函数：
      async def interceptor(request, handler) -> response

    拦截器的工作流程:
      1. 获取目标服务器的 OAuth Authorization 头
      2. 如果服务器不需要 OAuth，直接透传请求
      3. 如果需要 OAuth，将 Authorization 头注入请求的 headers 中
      4. 调用 handler 继续处理请求

    为什么用拦截器而非手动注入:
      langchain-mcp-adapters 的工具调用发生在框架内部，
      上层代码无法在每次调用前手动添加认证头。
      拦截器是框架提供的扩展点，可以在工具调用前后执行自定义逻辑。

    Args:
        extensions_config: 扩展配置对象

    Returns:
        拦截器异步函数，如果没有配置 OAuth 服务器则返回 None
    """
    token_manager = OAuthTokenManager.from_extensions_config(extensions_config)
    if not token_manager.has_oauth_servers():
        return None

    async def oauth_interceptor(request: Any, handler: Any) -> Any:
        # 获取目标服务器的认证头
        header = await token_manager.get_authorization_header(request.server_name)
        if not header:
            # 该服务器不需要 OAuth，直接透传
            return await handler(request)

        # 注入 Authorization 头到请求中
        updated_headers = dict(request.headers or {})
        updated_headers["Authorization"] = header
        # override 方法创建请求的副本，替换 headers
        return await handler(request.override(headers=updated_headers))

    return oauth_interceptor


async def get_initial_oauth_headers(extensions_config: ExtensionsConfig) -> dict[str, str]:
    """获取所有 OAuth 服务器的初始 Authorization 头。

    在 MCP 客户端初始化时调用，用于：
      - SSE/HTTP 连接建立时的首次认证
      - 工具发现（tool discovery）阶段的认证

    为什么需要在初始化时就获取令牌:
      MCP 协议在建立连接后立即进行工具发现（列出服务器提供的所有工具）。
      如果服务器要求认证，连接建立时就需要携带有效的令牌。
      拦截器只在后续的工具调用中生效，无法覆盖初始连接阶段。

    Args:
        extensions_config: 扩展配置对象

    Returns:
        {server_name: "Bearer xxx"} 映射，仅包含成功获取令牌的服务器
    """
    token_manager = OAuthTokenManager.from_extensions_config(extensions_config)
    if not token_manager.has_oauth_servers():
        return {}

    headers: dict[str, str] = {}
    for server_name in token_manager.oauth_server_names():
        headers[server_name] = await token_manager.get_authorization_header(server_name) or ""

    # 过滤掉获取失败的（空字符串）
    return {name: value for name, value in headers.items() if value}
