"""FastAPI 依赖注入中心 — app.state 单例获取器与运行时初始化。

本模块是 Gateway 的依赖注入枢纽，负责两个核心职责：

  1. **运行时初始化**（langgraph_runtime）：
     通过 AsyncExitStack 管理 LangGraph 运行时的所有单例生命周期，
     包括 StreamBridge、Checkpointer、Store、RunManager 等。
     在 app.py 的 lifespan 中通过 ``async with langgraph_runtime(app): yield`` 调用。

  2. **请求级依赖获取器**（get_*）：
     从 app.state 读取运行时单例供路由使用。缺失时返回 503 Service Unavailable，
     唯一例外是 get_store（返回 None）。

核心设计：
  - 使用 _require() 工厂函数批量创建类型安全的依赖获取器
  - 认证相关单例（LocalAuthProvider、SQLiteUserRepository）通过模块级
    缓存实现延迟初始化，避免循环导入
  - RunContext 的构建聚合了多个基础设施依赖
  - 用户身份解析从 Cookie → JWT → DB 查询 → Token 版本校验完整链路

依赖获取器一览：
  - get_stream_bridge     — SSE 事件桥接器
  - get_run_manager       — 运行管理器
  - get_checkpointer      — LangGraph 检查点存储
  - get_run_event_store   — 运行事件存储
  - get_feedback_repo     — 反馈仓库
  - get_run_store         — 运行记录仓库
  - get_store             — LangGraph Store（可能为 None）
  - get_thread_store      — 线程元数据存储
  - get_config            — 应用配置
  - get_run_context       — 完整运行上下文
  - get_local_provider    — 本地认证提供者
  - get_current_user_from_request — 从请求中获取当前认证用户
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, TypeVar, cast

from fastapi import FastAPI, HTTPException, Request
from langgraph.types import Checkpointer

from deerflow.config.app_config import AppConfig
from deerflow.persistence.feedback import FeedbackRepository
from deerflow.runtime import RunContext, RunManager, StreamBridge
from deerflow.runtime.events.store.base import RunEventStore
from deerflow.runtime.runs.store.base import RunStore

if TYPE_CHECKING:
    from app.gateway.auth.local_provider import LocalAuthProvider
    from app.gateway.auth.repositories.sqlite import SQLiteUserRepository
    from deerflow.persistence.thread_meta.base import ThreadMetaStore


T = TypeVar("T")


def get_config(request: Request) -> AppConfig:
    """返回存储在 app.state 上的应用级 AppConfig。

    Args:
        request: FastAPI 请求对象。

    Returns:
        AppConfig 实例。

    Raises:
        HTTPException 503: 配置不可用。
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="Configuration not available")
    return config


@asynccontextmanager
async def langgraph_runtime(app: FastAPI) -> AsyncGenerator[None, None]:
    """引导和清理所有 LangGraph 运行时单例。

    在 AsyncExitStack 中依次初始化以下组件：
      1. StreamBridge（SSE 事件桥接）
      2. 持久化引擎（PostgreSQL/SQLite 连接池）
      3. Checkpointer（LangGraph 检查点存储）
      4. Store（LangGraph 键值存储）
      5. 仓库实例（RunRepository、FeedbackRepository、ThreadStore）
      6. RunEventStore（运行事件存储）
      7. RunManager（运行生命周期管理）

    用法（在 app.py 中）::

        async with langgraph_runtime(app):
            yield
    """
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine_from_config
    from deerflow.runtime import make_store, make_stream_bridge
    from deerflow.runtime.checkpointer.async_provider import make_checkpointer
    from deerflow.runtime.events.store import make_run_event_store

    async with AsyncExitStack() as stack:
        config = getattr(app.state, "config", None)
        if config is None:
            raise RuntimeError("langgraph_runtime() requires app.state.config to be initialized")

        app.state.stream_bridge = await stack.enter_async_context(make_stream_bridge(config))

        # 在 Checkpointer 之前初始化持久化引擎，确保自动建库逻辑先执行（PostgreSQL 后端）
        await init_engine_from_config(config.database)

        app.state.checkpointer = await stack.enter_async_context(make_checkpointer(config))
        app.state.store = await stack.enter_async_context(make_store(config))

        # 初始化仓库 — 所有仓库共享同一个 session_factory
        sf = get_session_factory()
        if sf is not None:
            from deerflow.persistence.feedback import FeedbackRepository
            from deerflow.persistence.run import RunRepository

            app.state.run_store = RunRepository(sf)
            app.state.feedback_repo = FeedbackRepository(sf)
        else:
            # 无持久化引擎时使用内存实现
            from deerflow.runtime.runs.store.memory import MemoryRunStore

            app.state.run_store = MemoryRunStore()
            app.state.feedback_repo = None

        from deerflow.persistence.thread_meta import make_thread_store

        app.state.thread_store = make_thread_store(sf, app.state.store)

        # 运行事件存储（有独立的工厂方法，根据配置选择后端）
        run_events_config = getattr(config, "run_events", None)
        app.state.run_event_store = make_run_event_store(run_events_config)

        # RunManager 持有 RunStore 以支持持久化
        app.state.run_manager = RunManager(store=app.state.run_store)

        try:
            yield
        finally:
            await close_engine()


# ---------------------------------------------------------------------------
# 获取器 — 路由模块按请求调用
# ---------------------------------------------------------------------------


def _require(attr: str, label: str) -> Callable[[Request], T]:
    """创建 FastAPI 依赖函数：返回 app.state.<attr> 或抛出 503。

    工厂函数，为每个运行时单例生成类型安全的依赖获取器。

    Args:
        attr: app.state 上的属性名。
        label: 503 错误消息中的可读名称。

    Returns:
        FastAPI 依赖函数。
    """

    def dep(request: Request) -> T:
        val = getattr(request.app.state, attr, None)
        if val is None:
            raise HTTPException(status_code=503, detail=f"{label} not available")
        return cast(T, val)

    dep.__name__ = dep.__qualname__ = f"get_{attr}"
    return dep


# 批量创建运行时单例的依赖获取器
get_stream_bridge: Callable[[Request], StreamBridge] = _require("stream_bridge", "Stream bridge")
get_run_manager: Callable[[Request], RunManager] = _require("run_manager", "Run manager")
get_checkpointer: Callable[[Request], Checkpointer] = _require("checkpointer", "Checkpointer")
get_run_event_store: Callable[[Request], RunEventStore] = _require("run_event_store", "Run event store")
get_feedback_repo: Callable[[Request], FeedbackRepository] = _require("feedback_repo", "Feedback")
get_run_store: Callable[[Request], RunStore] = _require("run_store", "Run store")


def get_store(request: Request):
    """返回全局 Store（可能为 None，如果未配置）。

    与其他获取器不同，Store 是可选组件。

    Args:
        request: FastAPI 请求对象。

    Returns:
        LangGraph Store 实例或 None。
    """
    return getattr(request.app.state, "store", None)


def get_thread_store(request: Request) -> ThreadMetaStore:
    """返回线程元数据存储（SQL 或内存后端）。

    Args:
        request: FastAPI 请求对象。

    Returns:
        ThreadMetaStore 实例。

    Raises:
        HTTPException 503: 线程元数据存储不可用。
    """
    val = getattr(request.app.state, "thread_store", None)
    if val is None:
        raise HTTPException(status_code=503, detail="Thread metadata store not available")
    return val


def get_run_context(request: Request) -> RunContext:
    """从 app.state 单例构建 RunContext。

    返回包含基础设施依赖的*基础*上下文。

    Args:
        request: FastAPI 请求对象。

    Returns:
        聚合了 Checkpointer、Store、EventStore 等依赖的 RunContext。
    """
    config = get_config(request)
    return RunContext(
        checkpointer=get_checkpointer(request),
        store=get_store(request),
        event_store=get_run_event_store(request),
        run_events_config=getattr(config, "run_events", None),
        thread_store=get_thread_store(request),
        app_config=config,
    )


# ---------------------------------------------------------------------------
# 认证辅助函数（供 authz.py 和 auth 中间件使用）
# ---------------------------------------------------------------------------

# 模块级缓存的单例实例，避免每个请求重复创建
_cached_local_provider: LocalAuthProvider | None = None
_cached_repo: SQLiteUserRepository | None = None


def get_local_provider() -> LocalAuthProvider:
    """获取或创建缓存的 LocalAuthProvider 单例。

    必须在 init_engine_from_config() 之后调用 — 构建用户仓库
    需要共享的 session_factory。

    Returns:
        LocalAuthProvider 实例。

    Raises:
        RuntimeError: 持久化引擎未初始化时调用。
    """
    global _cached_local_provider, _cached_repo
    if _cached_repo is None:
        from app.gateway.auth.repositories.sqlite import SQLiteUserRepository
        from deerflow.persistence.engine import get_session_factory

        sf = get_session_factory()
        if sf is None:
            raise RuntimeError("get_local_provider() called before init_engine_from_config(); cannot access users table")
        _cached_repo = SQLiteUserRepository(sf)
    if _cached_local_provider is None:
        from app.gateway.auth.local_provider import LocalAuthProvider

        _cached_local_provider = LocalAuthProvider(repository=_cached_repo)
    return _cached_local_provider


async def get_current_user_from_request(request: Request):
    """从请求 Cookie 中获取当前认证用户。

    完整的认证链路：Cookie → JWT 解码 → DB 用户查询 → Token 版本校验。

    Args:
        request: FastAPI 请求对象。

    Returns:
        认证通过的 User 对象。

    Raises:
        HTTPException 401: 未认证、Token 无效、用户不存在或 Token 已过期。
    """
    from app.gateway.auth import decode_token
    from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse, TokenError, token_error_to_code

    access_token = request.cookies.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=AuthErrorCode.NOT_AUTHENTICATED, message="Not authenticated").model_dump(),
        )

    payload = decode_token(access_token)
    if isinstance(payload, TokenError):
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=token_error_to_code(payload), message=f"Token error: {payload.value}").model_dump(),
        )

    provider = get_local_provider()
    user = await provider.get_user(payload.sub)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=AuthErrorCode.USER_NOT_FOUND, message="User not found").model_dump(),
        )

    # Token 版本不匹配 → 密码已修改，旧 Token 失效
    if user.token_version != payload.ver:
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=AuthErrorCode.TOKEN_INVALID, message="Token revoked (password changed)").model_dump(),
        )

    return user


async def get_optional_user_from_request(request: Request):
    """从请求中获取可选的认证用户。

    与 get_current_user_from_request 不同，未认证时返回 None 而非抛出异常。

    Args:
        request: FastAPI 请求对象。

    Returns:
        User 对象，或 None（未认证）。
    """
    try:
        return await get_current_user_from_request(request)
    except HTTPException:
        return None


async def get_current_user(request: Request) -> str | None:
    """从请求 Cookie 中提取 user_id，未认证时返回 None。

    轻量级适配器，仅返回字符串 ID，适用于只需要身份标识的调用者
    （如 feedback.py）。需要完整用户对象的调用者应使用
    get_current_user_from_request 或 get_optional_user_from_request。

    Args:
        request: FastAPI 请求对象。

    Returns:
        用户 ID 字符串，或 None（未认证）。
    """
    user = await get_optional_user_from_request(request)
    return str(user.id) if user else None
