"""DeerFlow 应用表的 Alembic 迁移环境配置。

本模块 ONLY 管理 DeerFlow 自己的表（runs, threads_meta, cron_jobs, users）。
LangGraph 检查点的表由 LangGraph 自身管理 —— 它们有独立的
schema 生命周期，不能被 Alembic 触碰。

迁移模式:
  - offline: 生成 SQL 脚本（不连接数据库，适合审查和手动执行）
  - online:  连接数据库并执行迁移

技术要点:
  - render_as_batch=True: SQLite 的 ALTER TABLE 支持有限，
    batch 模式通过"创建新表→复制数据→删除旧表→重命名"的方式
    模拟完整的 ALTER TABLE 功能。
"""

from __future__ import annotations

import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.persistence.base import Base

# 导入所有模型，确保 metadata 被填充。
# 只有被导入过的模型才会出现在 Base.metadata 中，
# Alembic 才能检测到表结构变化。
try:
    import deerflow.persistence.models as models  # register ORM models with Base.metadata

    _ = models  # 防止 linter 报未使用警告
except ImportError:
    # 模型包不可用 —— 迁移将只能处理已有的 metadata
    logging.getLogger(__name__).warning("Could not import deerflow.persistence.models; Alembic may not detect all tables")

# Alembic 配置对象
config = context.config
# 设置日志配置（从 alembic.ini 文件中读取）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic 自动生成迁移时使用的元数据
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """以离线模式运行迁移。

    只生成 SQL 脚本，不连接数据库。
    适合用于:
      - 审查将要执行的 SQL
      - 在生产环境中手动执行迁移
      - CI/CD 流程中的迁移脚本生成
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,       # 将参数绑定值直接写入 SQL
        render_as_batch=True,     # 启用 batch 模式（SQLite 兼容）
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    """执行在线迁移（同步版本）。

    由 run_migrations_online() 通过 run_sync 调用，
    在异步连接的同步上下文中执行迁移操作。
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,     # SQLite ALTER TABLE 兼容必需
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """以在线模式运行迁移。

    创建临时异步引擎连接数据库，执行迁移后释放引擎。
    使用 run_sync 将同步的 Alembic 迁移代码桥接到异步环境中。
    """
    connectable = create_async_engine(config.get_main_option("sqlalchemy.url"))
    async with connectable.connect() as connection:
        # run_sync 在异步连接中运行同步函数
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


# 根据运行模式选择离线或在线迁移
if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
