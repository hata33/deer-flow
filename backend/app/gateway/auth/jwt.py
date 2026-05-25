"""JWT 令牌创建与验证 — DeerFlow 认证核心。

本模块实现了基于 HS256 算法的 JWT（JSON Web Token）令牌管理，
是认证子系统的核心组件。

主要功能：
  1. 创建访问令牌（create_access_token）：
     - 使用 HS256 算法签名
     - 包含用户 ID（sub）、过期时间（exp）、签发时间（iat）
     - 包含 Token 版本号（ver），用于密码修改后的旧 Token 失效

  2. 解码验证令牌（decode_token）：
     - 验证签名和过期时间
     - 返回类型安全的结果：成功返回 TokenPayload，失败返回 TokenError
     - 精确区分过期、签名无效和格式错误三种失败原因

核心设计：
  - Token 版本机制（ver 字段）：用户修改密码时 token_version 递增，
    认证时比对 JWT 中的 ver 和数据库中的 token_version，不匹配则拒绝
  - 函数式错误处理：decode_token 返回联合类型而非抛异常，
    调用者可通过 isinstance 判断结果类型
  - 密钥从 AuthConfig 获取，由 config.py 统一管理

Token 有效载荷（TokenPayload）：
  - sub: 用户 UUID 字符串
  - exp: 过期时间（UTC）
  - iat: 签发时间（UTC）
  - ver: Token 版本号，对应 User.token_version
"""

from datetime import UTC, datetime, timedelta

import jwt
from pydantic import BaseModel

from app.gateway.auth.config import get_auth_config
from app.gateway.auth.errors import TokenError


class TokenPayload(BaseModel):
    """JWT 令牌有效载荷。

    Attributes:
        sub: 用户 ID（UUID 字符串）。
        exp: 过期时间。
        iat: 签发时间，可选。
        ver: Token 版本号 — 必须与 User.token_version 匹配。
    """

    sub: str  # 用户 ID
    exp: datetime
    iat: datetime | None = None
    ver: int = 0  # Token 版本 — 必须与 User.token_version 匹配


def create_access_token(user_id: str, expires_delta: timedelta | None = None, token_version: int = 0) -> str:
    """创建 JWT 访问令牌。

    使用 HS256 算法签名，包含用户 ID、过期时间、签发时间和 Token 版本号。

    Args:
        user_id: 用户的 UUID 字符串。
        expires_delta: 可选的自定义过期时间增量，默认使用配置的 token_expiry_days。
        token_version: 用户当前的 token_version，用于密码修改后的旧 Token 失效。

    Returns:
        编码后的 JWT 字符串。
    """
    config = get_auth_config()
    expiry = expires_delta or timedelta(days=config.token_expiry_days)

    now = datetime.now(UTC)
    payload = {"sub": user_id, "exp": now + expiry, "iat": now, "ver": token_version}
    return jwt.encode(payload, config.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> TokenPayload | TokenError:
    """解码并验证 JWT 令牌。

    验证签名和过期时间，精确区分三种失败原因。

    Args:
        token: 待解码的 JWT 字符串。

    Returns:
        验证成功时返回 TokenPayload，失败时返回具体的 TokenError 变体。
    """
    config = get_auth_config()
    try:
        payload = jwt.decode(token, config.jwt_secret, algorithms=["HS256"])
        return TokenPayload(**payload)
    except jwt.ExpiredSignatureError:
        return TokenError.EXPIRED
    except jwt.InvalidSignatureError:
        return TokenError.INVALID_SIGNATURE
    except jwt.PyJWTError:
        return TokenError.MALFORMED
