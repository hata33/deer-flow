"""进程内内部认证 — Gateway 内部调用者的身份验证。

本模块为同进程内的内部调用（主要是 IM 频道 Worker → Gateway HTTP API）
提供认证机制。核心设计：

  - 每次 Gateway 进程启动时自动生成一个随机内部令牌（32 字节 URL-safe）
  - 内部调用通过 X-DeerFlow-Internal-Token 请求头携带该令牌
  - 令牌仅在进程内有效，无法跨进程伪造
  - AuthMiddleware 识别有效内部令牌后跳过 JWT 校验

使用场景：
  - IM 频道（飞书/Slack/Telegram/钉钉）的 Worker 通过 langgraph-sdk HTTP
    客户端调用 Gateway API 创建线程、运行 Agent 等
  - 这些调用发生在同一 Gateway 进程内，使用浏览器 Cookie 不合适
  - 内部令牌提供了轻量但安全的身份验证替代方案

关键特性：
  - 令牌使用 secrets.token_urlsafe(32) 生成，密码学安全
  - 使用 secrets.compare_digest 做常量时间比较，防止时序攻击
  - 内部用户拥有 DEFAULT_USER_ID 和 "internal" 系统角色
"""

from __future__ import annotations

import secrets
from types import SimpleNamespace

from deerflow.runtime.user_context import DEFAULT_USER_ID

# 内部认证令牌的 HTTP 头名称
INTERNAL_AUTH_HEADER_NAME = "X-DeerFlow-Internal-Token"
# 进程启动时自动生成的随机内部令牌
_INTERNAL_AUTH_TOKEN = secrets.token_urlsafe(32)


def create_internal_auth_headers() -> dict[str, str]:
    """返回认证同进程 Gateway 内部调用所需的请求头。

    用于 IM 频道 Worker 等内部组件调用 Gateway API 时设置请求头。

    Returns:
        包含内部认证令牌的请求头字典。
    """
    return {INTERNAL_AUTH_HEADER_NAME: _INTERNAL_AUTH_TOKEN}


def is_valid_internal_auth_token(token: str | None) -> bool:
    """验证给定的令牌是否与进程内部令牌匹配。

    使用常量时间比较（secrets.compare_digest）防止时序攻击。

    Args:
        token: 待验证的令牌字符串。

    Returns:
        True 表示令牌有效。
    """
    return bool(token) and secrets.compare_digest(token, _INTERNAL_AUTH_TOKEN)


def get_internal_user():
    """返回用于受信任内部频道调用的合成用户对象。

    内部用户使用 DEFAULT_USER_ID 和 "internal" 系统角色，
    绕过正常的 JWT 认证流程。

    Returns:
        包含 id 和 system_role 属性的 SimpleNamespace 对象。
    """
    return SimpleNamespace(id=DEFAULT_USER_ID, system_role="internal")
