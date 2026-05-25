"""LangGraph 兼容认证处理器 — 与 Gateway 共享 JWT 逻辑。

默认的 DeerFlow 运行时嵌入在 FastAPI Gateway 中；脚本和 Docker 部署
不加载此模块。它保留用于 LangGraph 工具链、Studio 或通过
langgraph.json 的 auth.path 实现的直接 LangGraph Server 兼容性。

当使用兼容路径时，本模块复用 Gateway 的 JWT 和 CSRF 规则，
确保两种模式一致地验证会话。

两层处理：
  1. @auth.authenticate — 验证 JWT Cookie，提取 user_id，
     并对状态变更方法（POST/PUT/DELETE/PATCH）执行 CSRF 校验
  2. @auth.on — 返回元数据过滤器，确保每个用户只能看到自己的线程

核心设计：
  - 使用 langgraph_sdk.Auth 注册认证和过滤钩子
  - CSRF 校验逻辑与 Gateway 的 CSRFMiddleware 保持一致
  - 认证链路与 Gateway 的 get_current_user_from_request 相同：
    Cookie → JWT 解码 → DB 查询 → Token 版本匹配
  - 线程属主过滤通过 metadata.user_id 字段实现
"""

import secrets

from langgraph_sdk import Auth

from app.gateway.auth.errors import TokenError
from app.gateway.auth.jwt import decode_token
from app.gateway.deps import get_local_provider

auth = Auth()

# 需要 CSRF 校验的 HTTP 方法（根据 RFC 7231，状态变更方法）
_CSRF_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def _check_csrf(request) -> None:
    """对状态变更请求执行 Double Submit Cookie CSRF 校验。

    与 Gateway 的 CSRFMiddleware 逻辑保持一致，确保通过 nginx 直接代理的
    LangGraph 路由拥有相同的 CSRF 防护。

    Args:
        request: 请求对象。

    Raises:
        Auth.exceptions.HTTPException 403: CSRF Token 缺失或不匹配。
    """
    method = getattr(request, "method", "") or ""
    if method.upper() not in _CSRF_METHODS:
        return

    cookie_token = request.cookies.get("csrf_token")
    header_token = request.headers.get("x-csrf-token")

    if not cookie_token or not header_token:
        raise Auth.exceptions.HTTPException(
            status_code=403,
            detail="CSRF token missing. Include X-CSRF-Token header.",
        )

    # 使用常量时间比较防止时序攻击
    if not secrets.compare_digest(cookie_token, header_token):
        raise Auth.exceptions.HTTPException(
            status_code=403,
            detail="CSRF token mismatch.",
        )


@auth.authenticate
async def authenticate(request):
    """验证会话 Cookie、解码 JWT 并检查 Token 版本。

    与 Gateway 的 get_current_user_from_request 相同的验证链路：
      Cookie → JWT 解码 → DB 查询 → Token 版本匹配。
    同时对状态变更方法执行 CSRF 校验。

    Args:
        request: 请求对象。

    Returns:
        已认证用户的 user_id 字符串。

    Raises:
        Auth.exceptions.HTTPException 401: 未认证或 Token 无效。
    """
    # 先检查 CSRF，即使 Cookie 携带有效 JWT 也要拒绝伪造的跨站请求
    _check_csrf(request)

    token = request.cookies.get("access_token")
    if not token:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Not authenticated",
        )

    payload = decode_token(token)
    if isinstance(payload, TokenError):
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Invalid token",
        )

    user = await get_local_provider().get_user(payload.sub)
    if user is None:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="User not found",
        )
    # Token 版本不匹配 → 密码已修改，旧 Token 失效
    if user.token_version != payload.ver:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Token revoked (password changed)",
        )

    return payload.sub


@auth.on
async def add_owner_filter(ctx: Auth.types.AuthContext, value: dict):
    """写入时注入 user_id 元数据；读取时按 user_id 过滤。

    Gateway 将线程属主信息存储为 metadata.user_id。
    此处理器确保 LangGraph Server 执行相同的隔离策略。

    Args:
        ctx: LangGraph 认证上下文，包含当前用户信息。
        value: 请求体字典，可设置 metadata。

    Returns:
        过滤条件字典，LangGraph 将其应用于搜索/读取/删除操作。
    """
    # 创建/更新时：将 user_id 写入元数据
    metadata = value.setdefault("metadata", {})
    metadata["user_id"] = ctx.user.identity

    # 返回过滤字典 — LangGraph 将其应用于搜索/读取/删除操作
    return {"user_id": ctx.user.identity}
