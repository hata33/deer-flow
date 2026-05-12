"""MCP HTTP/SSE 服务器的 OAuth token 支持。

支持两种 OAuth 授权流程：
- client_credentials: 客户端凭据授权（需要 client_id + client_secret）
- refresh_token: 刷新令牌授权（需要 refresh_token）

功能：
- Token 缓存与自动刷新（提前刷新，避免过期）
- 每服务器的并发控制（asyncio.Lock 防止并发刷新）
- 工具调用拦截器（每次工具调用时注入 Authorization 头）
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
    """缓存的 OAuth token。

    Attributes:
        access_token: 访问令牌。
        token_type: 令牌类型（如 "Bearer"）。
        expires_at: 过期时间。
    """

    access_token: str
    token_type: str
    expires_at: datetime


class OAuthTokenManager:
    """OAuth token 获取、缓存和刷新管理器。

    为每个 MCP 服务器维护独立的 token 缓存和刷新锁，
    支持提前刷新（refresh_skew_seconds）避免 token 在使用时过期。

    Attributes:
        _oauth_by_server: 服务器名到 OAuth 配置的映射。
        _tokens: 服务器名到缓存 token 的映射。
        _locks: 服务器名到 asyncio.Lock 的映射（防止并发刷新）。
    """

    def __init__(self, oauth_by_server: dict[str, McpOAuthConfig]):
        self._oauth_by_server = oauth_by_server
        self._tokens: dict[str, _OAuthToken] = {}
        self._locks: dict[str, asyncio.Lock] = {name: asyncio.Lock() for name in oauth_by_server}

    @classmethod
    def from_extensions_config(cls, extensions_config: ExtensionsConfig) -> OAuthTokenManager:
        """从扩展配置中提取所有启用了 OAuth 的 MCP 服务器配置。

        Args:
            extensions_config: 扩展配置。

        Returns:
            OAuthTokenManager 实例。
        """
        oauth_by_server: dict[str, McpOAuthConfig] = {}
        for server_name, server_config in extensions_config.get_enabled_mcp_servers().items():
            if server_config.oauth and server_config.oauth.enabled:
                oauth_by_server[server_name] = server_config.oauth
        return cls(oauth_by_server)

    def has_oauth_servers(self) -> bool:
        """是否存在需要 OAuth 认证的服务器。"""
        return bool(self._oauth_by_server)

    def oauth_server_names(self) -> list[str]:
        """返回需要 OAuth 认证的服务器名称列表。"""
        return list(self._oauth_by_server.keys())

    async def get_authorization_header(self, server_name: str) -> str | None:
        """获取指定服务器的 Authorization 请求头值。

        先检查缓存 token 是否有效，过期则通过锁保护刷新。
        使用双重检查模式（lock 外检查一次，lock 内再检查一次）避免不必要的刷新。

        Args:
            server_name: MCP 服务器名称。

        Returns:
            Authorization 头值（如 "Bearer xxx"），无需 OAuth 时返回 None。
        """
        oauth = self._oauth_by_server.get(server_name)
        if not oauth:
            return None

        token = self._tokens.get(server_name)
        if token and not self._is_expiring(token, oauth):
            return f"{token.token_type} {token.access_token}"

        # 通过锁保护刷新，防止并发重复请求
        lock = self._locks[server_name]
        async with lock:
            token = self._tokens.get(server_name)
            if token and not self._is_expiring(token, oauth):
                return f"{token.token_type} {token.access_token}"

            fresh = await self._fetch_token(oauth)
            self._tokens[server_name] = fresh
            logger.info(f"Refreshed OAuth access token for MCP server: {server_name}")
            return f"{fresh.token_type} {fresh.access_token}"

    @staticmethod
    def _is_expiring(token: _OAuthToken, oauth: McpOAuthConfig) -> bool:
        """检查 token 是否即将过期（考虑 refresh_skew_seconds 提前量）。"""
        now = datetime.now(UTC)
        return token.expires_at <= now + timedelta(seconds=max(oauth.refresh_skew_seconds, 0))

    async def _fetch_token(self, oauth: McpOAuthConfig) -> _OAuthToken:
        """通过 HTTP POST 请求获取新的 OAuth token。

        根据 grant_type 构建不同的请求参数，
        解析响应中的 token、类型和过期时间。

        Args:
            oauth: OAuth 配置。

        Returns:
            新获取的 OAuth token。

        Raises:
            ValueError: grant_type 不支持或缺少必要参数。
        """
        import httpx  # pyright: ignore[reportMissingImports]

        data: dict[str, str] = {
            "grant_type": oauth.grant_type,
            **oauth.extra_token_params,
        }

        if oauth.scope:
            data["scope"] = oauth.scope
        if oauth.audience:
            data["audience"] = oauth.audience

        if oauth.grant_type == "client_credentials":
            if not oauth.client_id or not oauth.client_secret:
                raise ValueError("OAuth client_credentials requires client_id and client_secret")
            data["client_id"] = oauth.client_id
            data["client_secret"] = oauth.client_secret
        elif oauth.grant_type == "refresh_token":
            if not oauth.refresh_token:
                raise ValueError("OAuth refresh_token grant requires refresh_token")
            data["refresh_token"] = oauth.refresh_token
            if oauth.client_id:
                data["client_id"] = oauth.client_id
            if oauth.client_secret:
                data["client_secret"] = oauth.client_secret
        else:
            raise ValueError(f"Unsupported OAuth grant type: {oauth.grant_type}")

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(oauth.token_url, data=data)
            response.raise_for_status()
            payload = response.json()

        # 从响应中提取 token，支持自定义字段名
        access_token = payload.get(oauth.token_field)
        if not access_token:
            raise ValueError(f"OAuth token response missing '{oauth.token_field}'")

        token_type = str(payload.get(oauth.token_type_field, oauth.default_token_type) or oauth.default_token_type)

        expires_in_raw = payload.get(oauth.expires_in_field, 3600)
        try:
            expires_in = int(expires_in_raw)
        except (TypeError, ValueError):
            expires_in = 3600

        expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in, 1))
        return _OAuthToken(access_token=access_token, token_type=token_type, expires_at=expires_at)


def build_oauth_tool_interceptor(extensions_config: ExtensionsConfig) -> Any | None:
    """构建 OAuth 工具调用拦截器，每次工具调用时注入 Authorization 头。

    无需 OAuth 的服务器不会生成拦截器。

    Args:
        extensions_config: 扩展配置。

    Returns:
        拦截器异步函数，无需 OAuth 时返回 None。
    """
    token_manager = OAuthTokenManager.from_extensions_config(extensions_config)
    if not token_manager.has_oauth_servers():
        return None

    async def oauth_interceptor(request: Any, handler: Any) -> Any:
        header = await token_manager.get_authorization_header(request.server_name)
        if not header:
            return await handler(request)

        updated_headers = dict(request.headers or {})
        updated_headers["Authorization"] = header
        return await handler(request.override(headers=updated_headers))

    return oauth_interceptor


async def get_initial_oauth_headers(extensions_config: ExtensionsConfig) -> dict[str, str]:
    """获取所有需要 OAuth 的 MCP 服务器的初始 Authorization 头。

    用于服务器连接建立阶段（工具发现/会话初始化）。

    Args:
        extensions_config: 扩展配置。

    Returns:
        服务器名到 Authorization 头值的映射。
    """
    token_manager = OAuthTokenManager.from_extensions_config(extensions_config)
    if not token_manager.has_oauth_servers():
        return {}

    headers: dict[str, str] = {}
    for server_name in token_manager.oauth_server_names():
        headers[server_name] = await token_manager.get_authorization_header(server_name) or ""

    return {name: value for name, value in headers.items() if value}
