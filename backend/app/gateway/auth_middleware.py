"""全局认证中间件 — 失败即关闭（fail-closed）的安全网。

本中间件是 Gateway 认证体系的第一道防线，对所有非公开路径的请求
执行强制身份校验。核心行为：

  1. 公开路径白名单：/health、/docs、/redoc、/openapi.json 以及
     登录/注册/登出等认证端点无需认证
  2. 内部认证令牌：携带 X-DeerFlow-Internal-Token 的请求被视为
     受信任的进程内调用（频道 Worker → Gateway），跳过 JWT 校验
  3. Cookie 校验：非公开路径必须有 access_token Cookie，否则返回 401
  4. JWT 严格验证：解码并校验 JWT 令牌，拒绝伪造/过期/无效令牌
  5. 用户上下文注入：认证成功后将 User 对象注入 request.state.user 和
     deerflow.runtime.user_context ContextVar，使仓库层自动按属主过滤

设计要点：
  - 即使路由层没有 @require_auth 装饰器，中间件也会拦截未认证请求
  - 细粒度权限检查（如"用户 A 不能读取用户 B 的线程"）在 authz.py 中处理
  - 使用 BaseHTTPMiddleware 实现，在 ASGI 栈中位于 CSRF 中间件之下

关键特性：
  - 内部认证令牌每次进程启动重新生成，无法跨进程伪造
  - JWT 解码失败时返回详细的 AuthErrorCode，而非笼统的 401
  - ContextVar 机制确保异步任务也能获取当前用户身份
"""

from collections.abc import Callable

from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse
from app.gateway.authz import _ALL_PERMISSIONS, AuthContext
from app.gateway.internal_auth import INTERNAL_AUTH_HEADER_NAME, get_internal_user, is_valid_internal_auth_token
from deerflow.runtime.user_context import reset_current_user, set_current_user

# 永远不需要认证的路径前缀
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
)

# 精确匹配的公开认证路径（登录/注册/状态检查等）。
# /api/v1/auth/me、/api/v1/auth/change-password 等端点不属于公开路径。
_PUBLIC_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/auth/login/local",
        "/api/v1/auth/register",
        "/api/v1/auth/logout",
        "/api/v1/auth/setup-status",
        "/api/v1/auth/initialize",
    }
)


def _is_public(path: str) -> bool:
    """判断请求路径是否属于公开路径（无需认证）。

    Args:
        path: 请求的 URL 路径。

    Returns:
        True 表示该路径无需认证即可访问。
    """
    stripped = path.rstrip("/")
    if stripped in _PUBLIC_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    """严格认证门控：拒绝没有有效会话的请求。

    对非公开路径执行两阶段检查：

    1. Cookie 存在性检查 — 缺失返回 401 NOT_AUTHENTICATED
    2. JWT 验证（通过 get_optional_user_from_request）— 返回 401
       TOKEN_INVALID（令牌缺失、格式错误、过期或用户不存在/已变更）

    认证成功后，将用户信息写入 request.state.user 和
    deerflow.runtime.user_context ContextVar，使仓库层的属主过滤器
    自动生效，无需每个路由都加 @require_auth 装饰器。
    需要按资源做授权检查的路由（如"用户 A 不能通过猜测 URL 读取用户 B 的线程"）
    应额外使用 @require_permission(..., owner_check=True) 显式校验。
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if _is_public(request.url.path):
            return await call_next(request)

        # 检查内部认证令牌（频道 Worker 等进程内调用）
        internal_user = None
        if is_valid_internal_auth_token(request.headers.get(INTERNAL_AUTH_HEADER_NAME)):
            internal_user = get_internal_user()

        # 非公开路径：要求会话 Cookie
        if internal_user is None and not request.cookies.get("access_token"):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": AuthErrorResponse(
                        code=AuthErrorCode.NOT_AUTHENTICATED,
                        message="Authentication required",
                    ).model_dump()
                },
            )

        # 严格 JWT 验证：在此处直接拒绝伪造/过期令牌返回 401，
        # 而不是静默放行。这关闭了"伪造 Cookie 绕过"的漏洞
        # （AUTH_TEST_PLAN 测试 7.5.8）：没有此检查的话，非隔离路由
        # 如 /api/models 会接受任何 Cookie 形式的字符串作为认证。
        #
        # 调用严格解析器以获取细粒度错误码（token_expired、token_invalid、
        # user_not_found 等），而不是被展平为一个通用错误码。
        # BaseHTTPMiddleware 不允许 HTTPException 上浮，所以在此捕获
        # 并渲染为 JSONResponse。
        from app.gateway.deps import get_current_user_from_request

        if internal_user is not None:
            user = internal_user
        else:
            try:
                user = await get_current_user_from_request(request)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

        # 同时写入 request.state.user（用于 ContextVar 模式）
        # 和 request.state.auth（使 @require_permission 的 "auth is None"
        # 分支短路，避免每个请求重复执行完整的 JWT 解码 + DB 查询）
        request.state.user = user
        request.state.auth = AuthContext(user=user, permissions=_ALL_PERMISSIONS)
        token = set_current_user(user)
        try:
            return await call_next(request)
        finally:
            reset_current_user(token)
