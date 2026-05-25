"""DeerFlow API Gateway — FastAPI 应用工厂与生命周期管理。

本模块是 Gateway 的入口文件，负责：
  1. 构建 FastAPI 应用实例（中间件栈、路由挂载、OpenAPI 文档配置）
  2. 管理应用生命周期（启动时初始化 LangGraph 运行时、引导管理员账号、
     启动 IM 频道服务；关闭时停止频道服务并回收资源）
  3. 首次启动引导：检测无管理员账号时提示访问 /setup 完成初始化
  4. 孤儿线程迁移：将"无认证 → 有认证"升级路径中遗留的
     LangGraph Store 线程归属到管理员账号

核心设计：
  - 使用 asynccontextmanager 管理生命周期，确保资源正确释放
  - 中间件栈顺序：AuthMiddleware → CSRFMiddleware → CORSMiddleware
  - 所有路由通过 include_router 挂载，各模块职责清晰
  - 关闭钩子有超时上限（5秒），防止 Worker 卡死影响 uvicorn 重载

关键特性：
  - 健康检查端点 GET /health 无需认证
  - 可通过环境变量 GATEWAY_ENABLE_DOCS 控制文档端点开关
  - 支持通过 GATEWAY_CORS_ORIGINS 配置跨域白名单
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.gateway.auth_middleware import AuthMiddleware
from app.gateway.config import get_gateway_config
from app.gateway.csrf_middleware import CSRFMiddleware, get_configured_cors_origins
from app.gateway.deps import langgraph_runtime
from app.gateway.routers import (
    agents,
    artifacts,
    assistants_compat,
    auth,
    channels,
    feedback,
    mcp,
    memory,
    models,
    runs,
    skills,
    suggestions,
    thread_runs,
    threads,
    uploads,
)
from deerflow.config import app_config as deerflow_app_config
from deerflow.config.app_config import apply_logging_level

AppConfig = deerflow_app_config.AppConfig
get_app_config = deerflow_app_config.get_app_config

# 默认日志配置；lifespan 会根据 config.yaml 的 log_level 覆盖
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

# 每个 shutdown 钩子允许运行的最长时间（秒）。
# 限制 Worker 退出时间，避免 uvicorn 的 reload supervisor 不断向
# 卡在等待清理的 Worker 发送信号。
_SHUTDOWN_HOOK_TIMEOUT_SECONDS = 5.0


async def _ensure_admin_user(app: FastAPI) -> None:
    """启动钩子：处理首次引导和孤儿线程迁移。

    启动后执行管理员账号检测：
      - 首次启动（无管理员）：不自动创建账号，提示操作员访问 /setup
      - 后续启动（管理员已存在）：运行一次性"无认证→有认证"孤儿线程迁移，
        将 LangGraph Store 中没有 user_id 的线程归属到管理员账号。

    SQL 持久层不需要额外迁移：四个 user_id 列（threads_meta、runs、
    run_events、feedback）随 auth 模块的 create_all 一起创建，
    新创建的表不会包含 NULL 属主的行。
    """
    from sqlalchemy import select

    from app.gateway.deps import get_local_provider
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.user.model import UserRow

    try:
        provider = get_local_provider()
    except RuntimeError:
        # 某些测试/启动路径下认证持久层尚未初始化，
        # 跳过管理员迁移工作而不是让 Gateway 启动失败。
        logger.warning("Auth persistence not ready; skipping admin bootstrap check")
        return

    sf = get_session_factory()
    if sf is None:
        return

    admin_count = await provider.count_admin_users()

    if admin_count == 0:
        logger.info("=" * 60)
        logger.info("  First boot detected — no admin account exists.")
        logger.info("  Visit /setup to complete admin account creation.")
        logger.info("=" * 60)
        return

    # 管理员已存在 — 运行孤儿线程迁移，将早于 auth 模块创建的
    # LangGraph 线程元数据归属到管理员账号。
    async with sf() as session:
        stmt = select(UserRow).where(UserRow.system_role == "admin").limit(1)
        row = (await session.execute(stmt)).scalar_one_or_none()

    if row is None:
        return  # 理论上不会发生（admin_count > 0），但保险起见跳过。

    admin_id = str(row.id)

    # LangGraph Store 孤儿迁移 — 非致命操作。
    # 覆盖"无认证→有认证"升级路径：将已有但未设置 user_id 的
    # LangGraph 线程元数据归属到管理员。
    store = getattr(app.state, "store", None)
    if store is not None:
        try:
            migrated = await _migrate_orphaned_threads(store, admin_id)
            if migrated:
                logger.info("Migrated %d orphan LangGraph thread(s) to admin", migrated)
        except Exception:
            logger.exception("LangGraph thread migration failed (non-fatal)")


async def _iter_store_items(store, namespace, *, page_size: int = 500):
    """对 LangGraph Store 命名空间进行分页异步迭代。

    替代旧版硬编码 limit=1000 的调用，使用游标式循环确保
    超过一页的孤儿数据不会被静默丢失。当页面为空或短页（最后一页）时终止。
    """
    offset = 0
    while True:
        batch = await store.asearch(namespace, limit=page_size, offset=offset)
        if not batch:
            return
        for item in batch:
            yield item
        if len(batch) < page_size:
            return
        offset += page_size


async def _migrate_orphaned_threads(store, admin_user_id: str) -> int:
    """将 LangGraph Store 中没有 user_id 的线程迁移到指定管理员账号。

    使用游标分页确保所有孤儿都被迁移，无论数量多少。
    返回迁移的行数。
    """
    migrated = 0
    async for item in _iter_store_items(store, ("threads",)):
        metadata = item.value.get("metadata", {})
        if not metadata.get("user_id"):
            metadata["user_id"] = admin_user_id
            item.value["metadata"] = metadata
            await store.aput(("threads",), item.key, item.value)
            migrated += 1
    return migrated


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期处理器。

    启动阶段：
      1. 加载应用配置并设置日志级别
      2. 初始化 LangGraph 运行时（StreamBridge、RunManager、Checkpointer、Store）
      3. 检测管理员引导状态，必要时迁移孤儿线程
      4. 启动 IM 频道服务（如已配置）

    关闭阶段：
      1. 停止 IM 频道服务（有超时保护）
      2. 关闭 LangGraph 运行时和持久化引擎
    """

    # 加载配置并在启动时检查必要的环境变量
    try:
        app.state.config = get_app_config()
        apply_logging_level(app.state.config.log_level)
        logger.info("Configuration loaded successfully")
    except Exception as e:
        error_msg = f"Failed to load configuration during gateway startup: {e}"
        logger.exception(error_msg)
        raise RuntimeError(error_msg) from e
    config = get_gateway_config()
    logger.info(f"Starting API Gateway on {config.host}:{config.port}")

    # 初始化 LangGraph 运行时组件（StreamBridge、RunManager、Checkpointer、Store）
    async with langgraph_runtime(app):
        logger.info("LangGraph runtime initialised")

        # 检查管理员引导状态，在管理员存在后迁移孤儿线程。
        # 必须在 langgraph_runtime 之后运行，因为需要 app.state.store 来做线程迁移
        await _ensure_admin_user(app)

        # 如果配置了 IM 频道，则启动频道服务
        try:
            from app.channels.service import start_channel_service

            channel_service = await start_channel_service(app.state.config)
            logger.info("Channel service started: %s", channel_service.get_status())
        except Exception:
            logger.exception("No IM channels configured or channel service failed to start")

        yield

        # 关闭时停止频道服务（有超时限制，防止 Worker 卡死）
        try:
            from app.channels.service import stop_channel_service

            await asyncio.wait_for(
                stop_channel_service(),
                timeout=_SHUTDOWN_HOOK_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Channel service shutdown exceeded %.1fs; proceeding with worker exit.",
                _SHUTDOWN_HOOK_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("Failed to stop channel service")

    logger.info("Shutting down API Gateway")


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。

    构建步骤：
      1. 根据配置决定是否启用文档端点（/docs、/redoc、/openapi.json）
      2. 注册中间件栈：Auth → CSRF → CORS
      3. 挂载所有 API 路由模块
      4. 注册健康检查端点

    Returns:
        配置完成的 FastAPI 应用实例。
    """
    config = get_gateway_config()
    docs_url = "/docs" if config.enable_docs else None
    redoc_url = "/redoc" if config.enable_docs else None
    openapi_url = "/openapi.json" if config.enable_docs else None

    app = FastAPI(
        title="DeerFlow API Gateway",
        description="""
## DeerFlow API Gateway

API Gateway for DeerFlow - A LangGraph-based AI agent backend with sandbox execution capabilities.

### Features

- **Models Management**: Query and retrieve available AI models
- **MCP Configuration**: Manage Model Context Protocol (MCP) server configurations
- **Memory Management**: Access and manage global memory data for personalized conversations
- **Skills Management**: Query and manage skills and their enabled status
- **Artifacts**: Access thread artifacts and generated files
- **Health Monitoring**: System health check endpoints

### Architecture

LangGraph-compatible requests are routed through nginx to this gateway.
This gateway provides runtime endpoints for agent runs plus custom endpoints for models, MCP configuration, skills, and artifacts.
        """,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        openapi_tags=[
            {
                "name": "models",
                "description": "Operations for querying available AI models and their configurations",
            },
            {
                "name": "mcp",
                "description": "Manage Model Context Protocol (MCP) server configurations",
            },
            {
                "name": "memory",
                "description": "Access and manage global memory data for personalized conversations",
            },
            {
                "name": "skills",
                "description": "Manage skills and their configurations",
            },
            {
                "name": "artifacts",
                "description": "Access and download thread artifacts and generated files",
            },
            {
                "name": "uploads",
                "description": "Upload and manage user files for threads",
            },
            {
                "name": "threads",
                "description": "Manage DeerFlow thread-local filesystem data",
            },
            {
                "name": "agents",
                "description": "Create and manage custom agents with per-agent config and prompts",
            },
            {
                "name": "suggestions",
                "description": "Generate follow-up question suggestions for conversations",
            },
            {
                "name": "channels",
                "description": "Manage IM channel integrations (Feishu, Slack, Telegram)",
            },
            {
                "name": "assistants-compat",
                "description": "LangGraph Platform-compatible assistants API (stub)",
            },
            {
                "name": "runs",
                "description": "LangGraph Platform-compatible runs lifecycle (create, stream, cancel)",
            },
            {
                "name": "health",
                "description": "Health check and system status endpoints",
            },
        ],
    )

    # 认证中间件：拒绝未认证的非公开路径请求（失败即关闭的安全网）
    app.add_middleware(AuthMiddleware)

    # CSRF 中间件：Double Submit Cookie 模式，保护状态变更请求
    app.add_middleware(CSRFMiddleware)

    # CORS 中间件：统一 nginx 入口默认同源。分源浏览器客户端需要在此显式配置白名单，
    # 确保 CORS 和 CSRF 的来源校验使用同一数据源。
    cors_origins = sorted(get_configured_cors_origins())
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ── 路由挂载 ──────────────────────────────────────────────────

    # Models API 挂载在 /api/models
    app.include_router(models.router)

    # MCP API 挂载在 /api/mcp
    app.include_router(mcp.router)

    # Memory API 挂载在 /api/memory
    app.include_router(memory.router)

    # Skills API 挂载在 /api/skills
    app.include_router(skills.router)

    # Artifacts API 挂载在 /api/threads/{thread_id}/artifacts
    app.include_router(artifacts.router)

    # Uploads API 挂载在 /api/threads/{thread_id}/uploads
    app.include_router(uploads.router)

    # Thread 清理 API 挂载在 /api/threads/{thread_id}
    app.include_router(threads.router)

    # Agents API 挂载在 /api/agents
    app.include_router(agents.router)

    # Suggestions API 挂载在 /api/threads/{thread_id}/suggestions
    app.include_router(suggestions.router)

    # Channels API 挂载在 /api/channels
    app.include_router(channels.router)

    # Assistants 兼容 API（LangGraph Platform 桩）
    app.include_router(assistants_compat.router)

    # Auth API 挂载在 /api/v1/auth
    app.include_router(auth.router)

    # Feedback API 挂载在 /api/threads/{thread_id}/runs/{run_id}/feedback
    app.include_router(feedback.router)

    # Thread Runs API（LangGraph Platform 兼容的运行生命周期）
    app.include_router(thread_runs.router)

    # 无状态 Runs API（无需预存线程即可 stream/wait）
    app.include_router(runs.router)

    @app.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        """健康检查端点。

        Returns:
            服务健康状态信息。
        """
        return {"status": "healthy", "service": "deer-flow-gateway"}

    return app


# 为 uvicorn 创建应用实例
app = create_app()
