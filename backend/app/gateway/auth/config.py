"""认证配置模块 — JWT 密钥管理与认证参数配置。

本模块管理 DeerFlow 认证子系统的核心配置，主要负责：

  1. JWT 签名密钥的生命周期管理：
     - 优先从环境变量 AUTH_JWT_SECRET 读取
     - 若未设置，自动生成 32 字节随机密钥并持久化到 .jwt_secret 文件
     - 密钥文件权限 0600，仅进程用户可读

  2. 认证参数配置：
     - Token 过期时间（默认 7 天，范围 1-30 天）
     - OAuth GitHub 集成参数（可选）

核心设计：
  - 密钥持久化确保重启后会话不失效（生产环境仍建议手动设置环境变量）
  - 使用 Pydantic BaseModel 确保类型安全和参数校验
  - 单例模式缓存配置实例，避免重复解析
  - .jwt_secret 文件使用原子写入（os.open + O_WRONLY|O_CREAT|O_TRUNC），
    避免写入中断导致文件损坏

环境变量：
  - AUTH_JWT_SECRET — JWT 签名密钥（生产环境必须手动设置）
"""

import logging
import os
import secrets

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# JWT 密钥持久化文件名（相对于 base_dir）
_SECRET_FILE = ".jwt_secret"


class AuthConfig(BaseModel):
    """JWT 和认证相关配置模型。启动时解析一次。

    注意：users 表现在位于由 deerflow.persistence.engine 管理的
    共享持久化数据库中。旧的 users_db_path 配置键已移除 —
    用户存储通过 config.database 配置，与其他表一致。

    Attributes:
        jwt_secret: JWT 签名密钥。必须通过 AUTH_JWT_SECRET 设置。
        token_expiry_days: Token 过期天数（1-30 天，默认 7 天）。
        oauth_github_client_id: GitHub OAuth 客户端 ID（可选）。
        oauth_github_client_secret: GitHub OAuth 客户端密钥（可选）。
    """

    jwt_secret: str = Field(
        ...,
        description="Secret key for JWT signing. MUST be set via AUTH_JWT_SECRET.",
    )
    token_expiry_days: int = Field(default=7, ge=1, le=30)
    oauth_github_client_id: str | None = Field(default=None)
    oauth_github_client_secret: str | None = Field(default=None)


# 缓存的全局配置实例
_auth_config: AuthConfig | None = None


def _load_or_create_secret() -> str:
    """从 {base_dir}/.jwt_secret 加载持久化的 JWT 密钥，或生成并持久化新密钥。

    密钥文件使用 0600 权限创建，确保仅进程用户可读。
    如果文件存在但为空或无法读取，生成新密钥。

    Returns:
        JWT 签名密钥字符串。

    Raises:
        RuntimeError: 无法读取或写入密钥文件。
    """
    from deerflow.config.paths import get_paths

    paths = get_paths()
    secret_file = paths.base_dir / _SECRET_FILE

    try:
        if secret_file.exists():
            secret = secret_file.read_text(encoding="utf-8").strip()
            if secret:
                return secret
    except OSError as exc:
        raise RuntimeError(f"Failed to read JWT secret from {secret_file}. Set AUTH_JWT_SECRET explicitly or fix DEER_FLOW_HOME/base directory permissions so DeerFlow can read its persisted auth secret.") from exc

    # 生成新的随机密钥
    secret = secrets.token_urlsafe(32)
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        # 原子创建 0600 权限文件，避免 write_text + chmod 之间的权限窗口
        fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(secret)
    except OSError as exc:
        raise RuntimeError(f"Failed to persist JWT secret to {secret_file}. Set AUTH_JWT_SECRET explicitly or fix DEER_FLOW_HOME/base directory permissions so DeerFlow can store a stable auth secret.") from exc
    return secret


def get_auth_config() -> AuthConfig:
    """获取全局 AuthConfig 实例。首次调用时从环境变量解析。

    密钥来源优先级：
      1. 环境变量 AUTH_JWT_SECRET
      2. 自动生成并持久化到 .jwt_secret 文件

    Returns:
        AuthConfig 实例。
    """
    global _auth_config
    if _auth_config is None:
        from dotenv import load_dotenv

        load_dotenv()
        jwt_secret = os.environ.get("AUTH_JWT_SECRET")
        if not jwt_secret:
            jwt_secret = _load_or_create_secret()
            os.environ["AUTH_JWT_SECRET"] = jwt_secret
            logger.warning(
                "⚠ AUTH_JWT_SECRET is not set — using an auto-generated secret "
                "persisted to .jwt_secret. Sessions will survive restarts. "
                "For production, add AUTH_JWT_SECRET to your .env file: "
                'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        _auth_config = AuthConfig(jwt_secret=jwt_secret)
    return _auth_config


def set_auth_config(config: AuthConfig) -> None:
    """设置全局 AuthConfig 实例（用于测试）。

    Args:
        config: 要设置的 AuthConfig 实例。
    """
    global _auth_config
    _auth_config = config
