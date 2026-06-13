"""授权装饰器和认证上下文 — DeerFlow 权限体系核心。

灵感来源于 LangGraph Auth 系统：
https://github.com/langchain-ai/langgraph/blob/main/libs/sdk-py/langgraph_sdk/auth/__init__.py

本模块实现了基于装饰器的授权检查机制，与 AuthMiddleware（认证中间件）
协同工作：
  - AuthMiddleware 负责身份验证（"你是谁？"）
  - authz.py 负责权限检查（"你能做什么？"）

使用方式：
  1. 在需要认证的路由上使用 @require_auth 装饰器
  2. 在需要权限检查的路由上使用 @require_permission("资源", "操作", ...)
  3. 装饰器链从下到上执行

示例::

    @router.get("/{thread_id}")
    @require_auth
    @require_permission("threads", "read", owner_check=True)
    async def get_thread(thread_id: str, request: Request):
        # 用户已通过认证并拥有 threads:read 权限
        ...

权限模型：
  - threads:read   — 查看线程
  - threads:write  — 创建/更新线程
  - threads:delete — 删除线程
  - runs:create    — 运行 Agent
  - runs:read      — 查看运行记录
  - runs:cancel    — 取消运行

核心设计：
  - AuthContext 存储在 request.state.auth 中，跨装饰器共享
  - 支持属主检查（owner_check）：验证当前用户是否拥有目标线程
  - 支持测试桩注入：单元测试可直接调用装饰过的函数而无需 FastAPI Request
  - 线程属主检查通过 ThreadMetaStore.check_access 实现：
    缺失行（旧线程）和 NULL user_id（共享线程）默认放行，
    仅已有且属主不同的行被拒绝
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from app.gateway.auth.models import User

P = ParamSpec("P")
T = TypeVar("T")


# 权限常量
class Permissions:
    """权限常量，采用 资源:操作 格式。"""

    # 线程相关权限
    THREADS_READ = "threads:read"
    THREADS_WRITE = "threads:write"
    THREADS_DELETE = "threads:delete"

    # 运行相关权限
    RUNS_CREATE = "runs:create"
    RUNS_READ = "runs:read"
    RUNS_CANCEL = "runs:cancel"


class AuthContext:
    """当前请求的认证上下文。

    在 @require_auth 装饰后存储在 request.state.auth 中。

    Attributes:
        user: 已认证的用户对象，匿名请求时为 None。
        permissions: 权限字符串列表（如 "threads:read"）。
    """

    __slots__ = ("user", "permissions")

    def __init__(self, user: User | None = None, permissions: list[str] | None = None):
        self.user = user
        self.permissions = permissions or []

    @property
    def is_authenticated(self) -> bool:
        """检查用户是否已认证。"""
        return self.user is not None

    def has_permission(self, resource: str, action: str) -> bool:
        """检查上下文是否拥有指定 资源:操作 的权限。

        Args:
            resource: 资源名称（如 "threads"）。
            action: 操作名称（如 "read"）。

        Returns:
            用户拥有该权限时返回 True。
        """
        permission = f"{resource}:{action}"
        return permission in self.permissions

    def require_user(self) -> User:
        """获取用户对象，未认证时抛出 401。

        Raises:
            HTTPException 401: 用户未认证。
        """
        if not self.user:
            raise HTTPException(status_code=401, detail="Authentication required")
        return self.user


def get_auth_context(request: Request) -> AuthContext | None:
    """从 request.state 获取 AuthContext。"""
    return getattr(request.state, "auth", None)


# 所有权限的完整列表，认证用户默认拥有全部权限
_ALL_PERMISSIONS: list[str] = [
    Permissions.THREADS_READ,
    Permissions.THREADS_WRITE,
    Permissions.THREADS_DELETE,
    Permissions.RUNS_CREATE,
    Permissions.RUNS_READ,
    Permissions.RUNS_CANCEL,
]


def _make_test_request_stub() -> Any:
    """创建最小化的请求桩对象，用于单元测试直接调用。

    当装饰过的路由处理函数在没有 FastAPI 请求注入的情况下被调用时使用。
    包含认证辅助函数访问的字段。
    """
    return SimpleNamespace(state=SimpleNamespace(), cookies={}, _deerflow_test_bypass_auth=True)


async def _authenticate(request: Request) -> AuthContext:
    """认证请求并返回 AuthContext。

    委托给 deps.get_optional_user_from_request() 执行 JWT→User 管线。
    匿名请求返回 user=None 的 AuthContext。
    """
    from app.gateway.deps import get_optional_user_from_request

    user = await get_optional_user_from_request(request)
    if user is None:
        return AuthContext(user=None, permissions=[])

    # 未来可将权限存储在用户记录中
    return AuthContext(user=user, permissions=_ALL_PERMISSIONS)


def require_auth[**P, T](func: Callable[P, T]) -> Callable[P, T]:
    """认证装饰器：验证请求身份并强制要求认证。

    独立于 AuthMiddleware 是否存在于 ASGI 栈中，对未认证请求
    一律抛出 HTTP 401。将解析后的 AuthContext 写入
    request.state.auth，供下游处理函数使用。

    必须放置在其他装饰器之上（在其他装饰器之后执行）。

    用法::

        @router.get("/{thread_id}")
        @require_auth  # 底层装饰器（在权限检查之后执行）
        @require_permission("threads", "read")
        async def get_thread(thread_id: str, request: Request):
            auth: AuthContext = request.state.auth
            ...

    Raises:
        HTTPException 401: 请求未认证。
        ValueError: 缺少 'request' 参数。
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        request = kwargs.get("request")
        if request is None:
            # 单元测试可能直接调用装饰过的处理函数，没有 FastAPI Request 对象。
            # 当被装饰的函数声明了 request 参数时，注入一个最小化的请求桩。
            if "request" in inspect.signature(func).parameters:
                kwargs["request"] = _make_test_request_stub()
            else:
                raise ValueError("require_auth decorator requires 'request' parameter")
            request = kwargs["request"]

        # 测试桩标记：跳过认证逻辑
        if getattr(request, "_deerflow_test_bypass_auth", False):
            return await func(*args, **kwargs)

        # 执行认证并设置上下文
        auth_context = await _authenticate(request)
        request.state.auth = auth_context

        if not auth_context.is_authenticated:
            raise HTTPException(status_code=401, detail="Authentication required")

        return await func(*args, **kwargs)

    return wrapper


def require_permission(
    resource: str,
    action: str,
    owner_check: bool = False,
    require_existing: bool = False,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """权限检查装饰器：验证用户是否拥有 资源:操作 权限。

    必须在 @require_auth 之后使用。

    Args:
        resource: 资源名称（如 "threads"、"runs"）。
        action: 操作名称（如 "read"、"write"、"delete"）。
        owner_check: 为 True 时验证当前用户是否为资源属主。
                     需要 thread_id 路径参数并执行属主检查。
        require_existing: 仅在 owner_check=True 时有意义。为 True 时，
                          缺失的 threads_meta 行视为拒绝（404），
                          而非"未追踪的旧线程，放行"。用于**破坏性/变更性**
                          路由（DELETE、PATCH、状态更新），防止已删除线程
                          通过缺失行路径被其他用户重新定向。

    用法::

        # 读取类操作：旧的不追踪线程被允许访问
        @require_permission("threads", "read", owner_check=True)
        async def get_thread(thread_id: str, request: Request):
            ...

        # 破坏性操作：线程行必须存在且属于调用者
        @require_permission("threads", "delete", owner_check=True, require_existing=True)
        async def delete_thread(thread_id: str, request: Request):
            ...

    Raises:
        HTTPException 401: 需要认证但用户匿名。
        HTTPException 403: 用户缺少权限。
        HTTPException 404: owner_check=True 但用户不拥有该线程。
        ValueError: owner_check=True 但缺少 thread_id 参数。
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            request = kwargs.get("request")
            if request is None:
                # 单元测试可能直接调用路由处理函数而不构造 FastAPI Request。
                # 当被装饰的函数声明了 request 参数时注入最小桩对象。
                if "request" in inspect.signature(func).parameters:
                    kwargs["request"] = _make_test_request_stub()
                else:
                    return await func(*args, **kwargs)
                request = kwargs["request"]

            # 测试桩标记：跳过权限检查
            if getattr(request, "_deerflow_test_bypass_auth", False):
                return await func(*args, **kwargs)

            auth: AuthContext = getattr(request.state, "auth", None)
            if auth is None:
                auth = await _authenticate(request)
                request.state.auth = auth

            if not auth.is_authenticated:
                raise HTTPException(status_code=401, detail="Authentication required")

            # 检查权限
            if not auth.has_permission(resource, action):
                raise HTTPException(
                    status_code=403,
                    detail=f"Permission denied: {resource}:{action}",
                )

            # 线程属主检查。
            #
            # 2.0-rc 将线程元数据移入 SQL 持久层（threads_meta 表）。
            # 通过 ThreadMetaStore.check_access 验证属主关系：
            #   - 缺失行（未追踪的旧线程）返回 True
            #   - user_id 为 NULL（共享/认证前数据）返回 True
            #   - 仅已有且 user_id 不同的行触发 404
            # 这是"严格拒绝而非严格放行"策略 — 只有显式归属其他用户的行才会被拒。
            if owner_check:
                from app.gateway.internal_auth import INTERNAL_OWNER_USER_ID_HEADER_NAME, INTERNAL_SYSTEM_ROLE

                thread_id = kwargs.get("thread_id")
                if thread_id is None:
                    raise ValueError("require_permission with owner_check=True requires 'thread_id' parameter")

                from app.gateway.deps import get_thread_store

                thread_store = get_thread_store(request)
                allowed = await thread_store.check_access(
                    thread_id,
                    str(auth.user.id),
                    require_existing=require_existing,
                )
                if not allowed and getattr(auth.user, "system_role", None) == INTERNAL_SYSTEM_ROLE:
                    # Trusted internal callers (channel workers) also act for
                    # the connection owner carried in X-DeerFlow-Owner-User-Id.
                    # Scope the check to that owner instead of bypassing it; a
                    # leaked internal token must not grant cross-user thread
                    # access. The header is honored only after ``auth`` proved
                    # the caller holds the internal token (mirrors
                    # get_trusted_internal_owner_user_id, which keys off the
                    # middleware-stamped ``request.state.user``).
                    header_owner = (request.headers.get(INTERNAL_OWNER_USER_ID_HEADER_NAME) or "").strip()
                    if header_owner:
                        allowed = await thread_store.check_access(
                            thread_id,
                            header_owner,
                            require_existing=require_existing,
                        )
                if not allowed:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Thread {thread_id} not found",
                    )

            return await func(*args, **kwargs)

        return wrapper

    return decorator
