"""用户表 ORM 模型。

定义了 users 表的结构，存储用户账户信息。
将此模型放在 harness 持久化包中（而非 app 层）的原因:
  - 与 threads_meta、runs、run_events、feedback 共享同一个数据库引擎
  - 一个 SQLite/Postgres 数据库，一个连接池
  - 统一的表初始化代码路径
  - 跨认证和持久化模块一致的异步会话管理

表设计要点:
  - id 使用 36 字符的 UUID 字符串：跨后端兼容（SQLite 不原生支持 UUID 类型）
  - email 有唯一索引：用于登录和查找
  - OAuth 字段有部分唯一索引：只约束非 NULL 的行，允许密码账户共存
  - system_role 使用字符串而非枚举：避免新增角色时需要 ALTER TABLE
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class UserRow(Base):
    """users 表的 ORM 模型。

    存储用户账户信息，支持密码认证和 OAuth 认证。

    字段说明:
      - id:             用户唯一标识（UUID 字符串，36 字符）
      - email:          用户邮箱（唯一，用于登录）
      - password_hash:  密码哈希（可空，OAuth 用户无密码）
      - system_role:    系统角色（"admin" 或 "user"）
      - created_at:     创建时间
      - oauth_provider: OAuth 提供商名称（如 "google"、"github"）
      - oauth_id:       OAuth 提供商中的用户 ID
      - needs_setup:    是否需要初始设置
      - token_version:  Token 版本号（用于强制登出）
    """

    __tablename__ = "users"

    # UUID 以 36 字符字符串存储，兼容 SQLite 和 PostgreSQL
    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # 邮箱字段：唯一且有索引，用于登录查找
    # 长度 320 符合 RFC 5321 的最大邮箱长度
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 可空：OAuth 用户没有密码

    # 系统角色：使用字符串而非枚举，避免新增角色时需要 ALTER TABLE
    # 值: "admin" | "user"
    system_role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    # ---- OAuth 关联字段（可选）----
    # 部分唯一索引确保每个 (provider, oauth_id) 对只有一个账户，
    # NULL/NULL 的行不受约束，允许纯密码账户共存。
    oauth_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)   # 如 "google", "github"
    oauth_id: Mapped[str | None] = mapped_column(String(128), nullable=True)        # OAuth 用户 ID

    # ---- 认证生命周期标志 ----
    needs_setup: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 新用户是否需要完成初始设置流程

    token_version: Mapped[int] = mapped_column(nullable=False, default=0)
    # Token 版本号：递增此值可使所有已发行的 JWT 失效（强制登出）

    __table_args__ = (
        # 部分唯一索引：只约束 oauth_provider 和 oauth_id 都非 NULL 的行
        # SQLite 使用 sqlite_where（WHERE 子句中的条件表达式）
        Index(
            "idx_users_oauth_identity",
            "oauth_provider",
            "oauth_id",
            unique=True,
            sqlite_where=text("oauth_provider IS NOT NULL AND oauth_id IS NOT NULL"),
        ),
    )
