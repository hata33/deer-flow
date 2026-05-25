"""DeerFlow 身份认证子系统 — 认证模块公共接口。

本子包实现了完整的身份认证功能，包括：

  - JWT 令牌管理（创建、解码、验证）
  - 密码哈希与验证（版本化格式：v1/v2）
  - 本地邮箱/密码认证提供者
  - 用户数据仓库抽象接口（支持 SQLite 等后端）
  - 认证错误码与结构化错误响应
  - 管理员密码重置 CLI 工具

模块结构：
  - config.py         — AuthConfig 配置，JWT 密钥管理
  - jwt.py            — JWT 令牌创建与解码
  - password.py       — 版本化密码哈希（bcrypt + SHA-256 预哈希）
  - models.py         — User、UserResponse Pydantic 模型
  - providers.py      — AuthProvider 抽象基类
  - local_provider.py — 本地邮箱/密码认证提供者实现
  - errors.py         — AuthErrorCode、TokenError 枚举
  - credential_file.py — 安全凭据文件写入
  - reset_admin.py    — CLI 管理员密码重置工具
  - repositories/     — 用户数据仓库接口与实现
    - base.py         — UserRepository 抽象接口
    - sqlite.py       — SQLite/SQLAlchemy 实现

核心设计：
  - 提供者模式（Provider Pattern）：AuthProvider 抽象基类支持
    扩展更多认证方式（OAuth、LDAP 等）
  - 仓库模式（Repository Pattern）：UserRepository 抽象接口
    支持不同存储后端（当前仅 SQLite）
  - 版本化密码哈希：v1（原始 bcrypt）→ v2（SHA-256 预哈希 + bcrypt），
    自动检测并透明升级
  - Token 版本机制：密码修改时递增 token_version，使旧 JWT 失效
"""

from app.gateway.auth.config import AuthConfig, get_auth_config, set_auth_config
from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse, TokenError
from app.gateway.auth.jwt import TokenPayload, create_access_token, decode_token
from app.gateway.auth.local_provider import LocalAuthProvider
from app.gateway.auth.models import User, UserResponse
from app.gateway.auth.password import hash_password, verify_password
from app.gateway.auth.providers import AuthProvider
from app.gateway.auth.repositories.base import UserRepository

__all__ = [
    # 配置
    "AuthConfig",
    "get_auth_config",
    "set_auth_config",
    # 错误
    "AuthErrorCode",
    "AuthErrorResponse",
    "TokenError",
    # JWT
    "TokenPayload",
    "create_access_token",
    "decode_token",
    # 密码
    "hash_password",
    "verify_password",
    # 模型
    "User",
    "UserResponse",
    # 提供者
    "AuthProvider",
    "LocalAuthProvider",
    # 仓库
    "UserRepository",
]
