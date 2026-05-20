"""异步 SQLAlchemy 引擎生命周期管理。

本模块在 Gateway 启动时初始化数据库引擎，为各 Repository 提供会话工厂，
在 Gateway 关闭时释放所有连接。

支持三种后端:
  - memory:   纯内存模式，init_engine 为空操作，get_session_factory() 返回 None。
              各 Repository 必须检查 None 并回退到内存实现。
  - sqlite:   本地 SQLite 文件数据库，启用 WAL 模式以支持并发读写。
  - postgres: PostgreSQL 数据库，使用连接池和 asyncpg 驱动。

引擎初始化流程:
  1. 根据后端类型创建 AsyncEngine
  2. 创建 async_session_factory（每个方法获取独立短生命周期会话）
  3. 自动建表（开发便利功能，生产环境应使用 Alembic 迁移）
  4. PostgreSQL 特有：如果目标数据库不存在，自动连接 postgres 库并 CREATE DATABASE
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def _json_serializer(obj: object) -> str:
    """JSON 序列化器，设置 ensure_ascii=False 以支持中文字符。

    作用：确保 JSON 列中存储的中文内容不会被转义为 \\uXXXX 格式，
    保持可读性。
    """
    return json.dumps(obj, ensure_ascii=False)


logger = logging.getLogger(__name__)

# 全局单例：异步引擎和会话工厂
# 模块级变量，整个进程共享同一个引擎实例
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def _auto_create_postgres_db(url: str) -> None:
    """自动创建 PostgreSQL 数据库。

    连接到 PostgreSQL 服务器的默认 ``postgres`` 维护数据库，
    执行 CREATE DATABASE 命令。目标数据库名从 url 中提取。

    为什么需要这个函数：
      首次部署时目标数据库可能还不存在，直接连接会报错。
      此函数自动检测并创建，简化部署流程。

    技术要点：
      CREATE DATABASE 不能在事务中执行，因此使用 AUTOCOMMIT 隔离级别。
      这也是为什么需要单独创建一个维护连接而不是复用主引擎。
    """
    from sqlalchemy import text
    from sqlalchemy.engine.url import make_url

    parsed = make_url(url)
    db_name = parsed.database
    if not db_name:
        raise ValueError("Cannot auto-create database: no database name in URL")

    # 将 URL 中的数据库名替换为默认的 'postgres' 维护库
    maint_url = parsed.set(database="postgres")
    # AUTOCOMMIT 隔离级别允许在事务外执行 CREATE DATABASE
    maint_engine = create_async_engine(maint_url, isolation_level="AUTOCOMMIT")
    try:
        async with maint_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        logger.info("Auto-created PostgreSQL database: %s", db_name)
    finally:
        # 确保维护引擎被释放，避免连接泄漏
        await maint_engine.dispose()


async def init_engine(
    backend: str,
    *,
    url: str = "",
    echo: bool = False,
    pool_size: int = 5,
    sqlite_dir: str = "",
) -> None:
    """创建异步引擎和会话工厂，然后自动建表。

    这是数据库初始化的核心入口。根据后端类型选择不同的配置策略，
    创建全局单例引擎和会话工厂。

    Args:
        backend:    后端类型："memory"（纯内存）、"sqlite" 或 "postgres"
        url:        SQLAlchemy 异步连接 URL（sqlite/postgres 需要）
        echo:       是否将 SQL 语句输出到日志（调试用）
        pool_size:  PostgreSQL 连接池大小
        sqlite_dir: SQLite 数据库文件所在目录（确保目录存在）

    引擎创建逻辑:
      - memory:   不创建引擎，直接返回（内存模式不需要数据库）
      - sqlite:   创建引擎 + 注册 WAL 模式监听器（每次新连接时执行 PRAGMA）
      - postgres: 创建引擎 + 连接池 + pool_pre_ping（自动检测断开的连接）
    """
    global _engine, _session_factory

    # ---- memory 模式：跳过引擎创建 ----
    if backend == "memory":
        logger.info("Persistence backend=memory -- ORM engine not initialized")
        return

    # ---- postgres 模式：检查 asyncpg 驱动是否已安装 ----
    if backend == "postgres":
        try:
            import asyncpg  # noqa: F401
        except ImportError:
            raise ImportError(
                "database.backend is set to 'postgres' but asyncpg is not installed.\n"
                "Install it with:\n"
                "    cd backend && uv sync --all-packages --extra postgres\n"
                "On the next `make dev` the postgres extra is auto-detected from\n"
                "config.yaml (database.backend: postgres) and reinstalled, so it\n"
                "will not be wiped again. Set UV_EXTRAS=postgres in .env to opt in\n"
                "explicitly. Or switch to backend: sqlite in config.yaml for\n"
                "single-node deployment."
            ) from None

    # ---- sqlite 模式：创建引擎并配置 WAL ----
    if backend == "sqlite":
        import os

        from sqlalchemy import event

        # 确保数据库文件所在目录存在
        os.makedirs(sqlite_dir or ".", exist_ok=True)
        _engine = create_async_engine(url, echo=echo, json_serializer=_json_serializer)

        # 在每个新连接上启用 WAL 模式
        # SQLite PRAGMA 设置是连接级别的，不能全局设置一次，
        # 因此需要通过事件监听器在每个新连接建立时执行。
        #
        # WAL（Write-Ahead Logging）模式的好处:
        #   - 允许并发读取和写入，不会相互阻塞
        #   - 是 SQLite 生产环境的标准推荐配置
        #
        # synchronous=NORMAL 与 WAL 配合使用:
        #   - 只在 WAL 检查点边界执行 fsync，而不是每次提交都 fsync
        #   - 在安全性和性能之间取得平衡
        #
        # foreign_keys=ON:
        #   - 启用外键约束检查（SQLite 默认关闭）
        #   - 确保数据引用完整性
        #
        # 注意：不设置 busy_timeout，因为 Python 的 sqlite3 驱动
        # 默认已有 5 秒的忙等超时，重复设置无意义。
        @event.listens_for(_engine.sync_engine, "connect")
        def _enable_sqlite_wal(dbapi_conn, _record):  # noqa: ARG001 — SQLAlchemy contract
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL;")       # 启用 WAL 日志模式
                cursor.execute("PRAGMA synchronous=NORMAL;")      # 降低同步频率提升性能
                cursor.execute("PRAGMA foreign_keys=ON;")         # 启用外键约束
            finally:
                cursor.close()

    # ---- postgres 模式：创建引擎并配置连接池 ----
    elif backend == "postgres":
        _engine = create_async_engine(
            url,
            echo=echo,
            pool_size=pool_size,          # 连接池大小，控制同时活跃的数据库连接数
            pool_pre_ping=True,           # 每次从池中取连接时先 ping 一下，自动检测断开的连接
            json_serializer=_json_serializer,
        )
    else:
        raise ValueError(f"Unknown persistence backend: {backend!r}")

    # 创建会话工厂
    # expire_on_commit=False：提交后不自动过期对象属性，
    # 避免在提交后访问属性时触发额外的 SELECT 查询
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    # ---- 自动建表（开发便利功能）----
    # 生产环境应使用 Alembic 迁移工具管理表结构变更
    from deerflow.persistence.base import Base

    # 导入所有模型，使 Base.metadata 发现并注册所有表定义
    # 如果模型包尚不存在（脚手架阶段），跳过也不会报错
    try:
        import deerflow.persistence.models  # noqa: F401
    except ImportError:
        logger.debug("deerflow.persistence.models not found; skipping auto-create tables")

    # 尝试创建所有未存在的表
    try:
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        if backend == "postgres" and "does not exist" in str(exc):
            # PostgreSQL 特有处理：目标数据库不存在
            # 1. 自动创建数据库
            await _auto_create_postgres_db(url)
            # 2. 释放旧引擎（连接到不存在的库）
            await _engine.dispose()
            # 3. 重新创建引擎（连接到刚创建的库）
            _engine = create_async_engine(url, echo=echo, pool_size=pool_size, pool_pre_ping=True, json_serializer=_json_serializer)
            _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
            # 4. 再次尝试建表
            async with _engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        else:
            raise

    logger.info("Persistence engine initialized: backend=%s", backend)


async def init_engine_from_config(config) -> None:
    """从配置对象便捷初始化引擎。

    作用：将 DatabaseConfig 的字段映射到 init_engine 的参数，
    避免调用方手动提取每个配置项。
    """
    if config.backend == "memory":
        await init_engine("memory")
        return
    await init_engine(
        backend=config.backend,
        url=config.app_sqlalchemy_url,
        echo=config.echo_sql,
        pool_size=config.pool_size,
        sqlite_dir=config.sqlite_dir if config.backend == "sqlite" else "",
    )


def get_session_factory() -> async_sessionmaker[AsyncSession] | None:
    """返回异步会话工厂。如果后端为 memory 则返回 None。

    各 Repository 通过此工厂创建短生命周期的会话，
    每个方法使用独立的会话，避免长事务持有连接。
    """
    return _session_factory


def get_engine() -> AsyncEngine | None:
    """返回当前引擎实例。如果未初始化则返回 None。"""
    return _engine


async def close_engine() -> None:
    """关闭引擎，释放所有数据库连接。

    在 Gateway 关闭时调用，确保优雅地释放连接池中的所有连接，
    避免连接泄漏。
    """
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("Persistence engine closed")
    _engine = None
    _session_factory = None
