"""用户 Pydantic 模型 — 认证子系统的数据结构定义。

本模块定义了认证子系统中使用的 Pydantic 数据模型，是内部表示
和外部 API 响应之间的桥梁。

两类核心模型：

  1. User（内部用户模型）：
     - 完整的用户数据表示，包含密码哈希、OAuth 信息等敏感字段
     - 用于认证子系统内部的数据传递和存储
     - 支持 from_attributes=True，可直接从 SQLAlchemy ORM 行转换
     - 包含 token_version 字段用于密码修改后的 JWT 失效机制

  2. UserResponse（API 响应模型）：
     - 安全的用户信息表示，不包含密码哈希等敏感字段
     - 用于 /api/v1/auth/me 等端点的响应格式
     - 仅暴露 id、email、system_role 和 needs_setup

核心设计：
  - UUID 主键：使用 uuid4 自动生成，全局唯一
  - EmailStr 类型：Pydantic 自动验证邮箱格式
  - 系统角色：仅支持 "admin" 和 "user" 两种角色（Literal 类型约束）
  - 时区感知时间：所有时间戳使用 UTC 时区
  - Token 版本：每次密码修改递增，用于使旧 JWT 失效
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, EmailStr, Field


def _utc_now() -> datetime:
    """返回当前 UTC 时间（带时区信息）。

    用于模型字段的默认值工厂，确保时间戳一致性。

    Returns:
        带时区信息的当前 UTC 时间。
    """
    return datetime.now(UTC)


class User(BaseModel):
    """内部用户数据模型。

    包含完整的用户信息，用于认证子系统内部的数据传递。
    支持 from_attributes=True，可直接从 SQLAlchemy ORM 行转换。

    Attributes:
        id: 主键 UUID。
        email: 唯一邮箱地址。
        password_hash: bcrypt 哈希值，OAuth 用户为 None。
        system_role: 系统角色（"admin" 或 "user"）。
        created_at: 创建时间（UTC）。
        oauth_provider: OAuth 提供者名称（如 'github'、'google'），可选。
        oauth_id: OAuth 提供者中的用户 ID，可选。
        needs_setup: 为 True 时表示重置账号需要完成首次设置。
        token_version: 密码修改时递增，用于使旧 JWT 失效。
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4, description="Primary key")
    email: EmailStr = Field(..., description="Unique email address")
    password_hash: str | None = Field(None, description="bcrypt hash, nullable for OAuth users")
    system_role: Literal["admin", "user"] = Field(default="user")
    created_at: datetime = Field(default_factory=_utc_now)

    # OAuth 关联（可选）
    oauth_provider: str | None = Field(None, description="e.g. 'github', 'google'")
    oauth_id: str | None = Field(None, description="User ID from OAuth provider")

    # 认证生命周期
    needs_setup: bool = Field(default=False, description="True when a reset account must complete setup")
    token_version: int = Field(default=0, description="Incremented on password change to invalidate old JWTs")


class UserResponse(BaseModel):
    """用户信息 API 响应模型。

    安全的用户信息表示，不包含密码哈希等敏感字段。
    用于 /api/v1/auth/me 等端点的响应格式。

    Attributes:
        id: 用户 ID 字符串。
        email: 用户邮箱。
        system_role: 系统角色（"admin" 或 "user"）。
        needs_setup: 是否需要完成首次设置。
    """

    id: str
    email: str
    system_role: Literal["admin", "user"]
    needs_setup: bool = False
