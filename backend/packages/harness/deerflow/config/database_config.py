"""统一数据库后端配置。

本模块控制 DeerFlow 两个持久化子系统的数据库后端：
1. LangGraph Checkpointer（对话状态持久化）
2. DeerFlow 应用持久层（运行记录、线程元数据、用户、反馈等）

用户只需配置一个后端，系统负责处理物理层面的细节差异。

## 三种后端模式

### memory 模式
- Checkpointer 使用 MemorySaver（内存）
- 应用层使用内存存储
- 不初始化任何数据库
- 重启后数据丢失，仅用于开发和测试

### sqlite 模式
- Checkpointer 和应用共享单个 .db 文件
- 文件路径：{sqlite_dir}/deerflow.db
- 每个 connection 启用 WAL 日志模式，允许并发读和单个写
- 写入冲突通过 sqlite3 的 busy timeout（默认 5 秒）等待而非立即失败

### postgres 模式
- 两者使用相同的数据库 URL
- 但维护独立的连接池，生命周期不同
- 适用于生产多节点部署

## 环境变量

敏感值（如 postgres_url）应在 config.yaml 中使用 $VAR 语法引用环境变量：

    database:
      backend: postgres
      postgres_url: $DATABASE_URL

$VAR 解析由 AppConfig.resolve_env_variables() 在本配置实例化之前完成，
因此 DatabaseConfig 自身不需要处理环境变量。
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    """统一数据库配置模型。

    字段说明：
    - backend: 存储后端类型（memory/sqlite/postgres）
    - sqlite_dir: SQLite 数据库文件目录（仅 sqlite 模式）
    - postgres_url: PostgreSQL 连接 URL（仅 postgres 模式）
    - echo_sql: 是否将 SQL 语句输出到日志（仅调试用）
    - pool_size: 应用 ORM 引擎的连接池大小（仅 postgres 模式）
    """

    backend: Literal["memory", "sqlite", "postgres"] = Field(
        default="memory",
        description=("Storage backend for both checkpointer and application data. 'memory' for development (no persistence across restarts), 'sqlite' for single-node deployment, 'postgres' for production multi-node deployment."),
    )
    sqlite_dir: str = Field(
        default=".deer-flow/data",
        description=("Directory for the SQLite database file. Both checkpointer and application data share {sqlite_dir}/deerflow.db."),
    )
    postgres_url: str = Field(
        default="",
        description=(
            "PostgreSQL connection URL, shared by checkpointer and app. "
            "Use $DATABASE_URL in config.yaml to reference .env. "
            "Example: postgresql://user:pass@host:5432/deerflow "
            "(the +asyncpg driver suffix is added automatically where needed)."
        ),
    )
    echo_sql: bool = Field(
        default=False,
        description="Echo all SQL statements to log (debug only).",
    )
    pool_size: int = Field(
        default=5,
        description="Connection pool size for the app ORM engine (postgres only).",
    )

    # -- 派生属性（非用户配置，由上面的字段计算得出） --

    @property
    def _resolved_sqlite_dir(self) -> str:
        """将 sqlite_dir 解析为绝对路径（相对于 CWD）。

        用户在 config.yaml 中可能写相对路径（如 .deer-flow/data），
        这里将其解析为绝对路径，确保后续拼接正确。
        """
        from pathlib import Path

        return str(Path(self.sqlite_dir).resolve())

    @property
    def sqlite_path(self) -> str:
        """统一的 SQLite 文件路径，checkpointer 和应用共享。

        路径格式：{resolved_sqlite_dir}/deerflow.db
        """
        return os.path.join(self._resolved_sqlite_dir, "deerflow.db")

    # 向后兼容别名：旧代码可能直接访问这些属性
    @property
    def checkpointer_sqlite_path(self) -> str:
        """LangGraph Checkpointer 的 SQLite 文件路径（sqlite_path 的别名）。"""
        return self.sqlite_path

    @property
    def app_sqlite_path(self) -> str:
        """应用 ORM 数据的 SQLite 文件路径（sqlite_path 的别名）。"""
        return self.sqlite_path

    @property
    def app_sqlalchemy_url(self) -> str:
        """应用 ORM 引擎的 SQLAlchemy 异步 URL。

        根据后端类型自动生成：
        - sqlite: sqlite+aiosqlite:///{sqlite_path}
        - postgres: postgresql+asyncpg://...（自动添加 asyncpg 驱动）
        - memory: 抛出 ValueError（没有对应的 SQLAlchemy URL）
        """
        if self.backend == "sqlite":
            return f"sqlite+aiosqlite:///{self.sqlite_path}"
        if self.backend == "postgres":
            url = self.postgres_url
            # 自动将 postgresql:// 替换为 postgresql+asyncpg://
            # 用户在配置中写 postgresql:// 即可，不需要记住驱动名
            if url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            return url
        raise ValueError(f"No SQLAlchemy URL for backend={self.backend!r}")
