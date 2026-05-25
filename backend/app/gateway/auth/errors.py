"""认证模块类型化错误定义 — 结构化错误码与响应。

本模块定义了认证子系统中所有错误条件的枚举和响应格式，
取代裸字符串 detail，提供机器可读的错误码。

三类核心定义：

  1. AuthErrorCode（认证错误码）：
     涵盖所有认证失败场景的穷举枚举，包括：
     - 凭据无效、Token 过期、Token 无效
     - 用户不存在、邮箱已注册、提供者未找到
     - 未认证、系统已初始化

  2. TokenError（Token 解码错误）：
     JWT 解码失败的具体原因枚举，用于精确区分：
     - Token 过期
     - 签名无效
     - 格式错误

  3. AuthErrorResponse（结构化错误响应）：
     标准化的错误响应格式，包含错误码和可读消息。
     前端可根据错误码实现差异化处理（如 Token 过期自动刷新）。

设计要点：
  - 使用 StrEnum 确保序列化值为字符串而非枚举对象
  - token_error_to_code 提供 TokenError → AuthErrorCode 的单一映射
  - 所有 HTTP 错误响应使用统一的 AuthErrorResponse 格式
"""

from enum import StrEnum

from pydantic import BaseModel


class AuthErrorCode(StrEnum):
    """认证错误条件穷举枚举。

    每个成员对应一种具体的认证失败场景，前端可根据错误码
    实现差异化处理逻辑。
    """

    INVALID_CREDENTIALS = "invalid_credentials"       # 邮箱/密码不匹配
    TOKEN_EXPIRED = "token_expired"                   # JWT 已过期
    TOKEN_INVALID = "token_invalid"                   # JWT 无效（签名/格式/版本）
    USER_NOT_FOUND = "user_not_found"                 # 用户不存在
    EMAIL_ALREADY_EXISTS = "email_already_exists"     # 邮箱已被注册
    PROVIDER_NOT_FOUND = "provider_not_found"         # 认证提供者未找到
    NOT_AUTHENTICATED = "not_authenticated"           # 未认证（缺少 Cookie）
    SYSTEM_ALREADY_INITIALIZED = "system_already_initialized"  # 系统已完成初始化


class TokenError(StrEnum):
    """JWT 解码失败原因穷举枚举。

    用于精确区分 Token 验证失败的具体原因，
    并通过 token_error_to_code 映射到对应的 AuthErrorCode。
    """

    EXPIRED = "expired"                # Token 已过期
    INVALID_SIGNATURE = "invalid_signature"  # 签名验证失败
    MALFORMED = "malformed"            # Token 格式错误


class AuthErrorResponse(BaseModel):
    """结构化错误响应 — 替代裸 detail 字符串。

    统一的错误响应格式，前端可根据 code 实现差异化处理。

    Attributes:
        code: 认证错误码。
        message: 人类可读的错误描述。
    """

    code: AuthErrorCode
    message: str


def token_error_to_code(err: TokenError) -> AuthErrorCode:
    """将 TokenError 映射为 AuthErrorCode — 单一映射来源。

    确保所有 Token 错误到认证错误码的转换逻辑集中在一处，
    避免散落在多个调用点导致不一致。

    Args:
        err: Token 错误枚举值。

    Returns:
        对应的 AuthErrorCode。
    """
    if err == TokenError.EXPIRED:
        return AuthErrorCode.TOKEN_EXPIRED
    return AuthErrorCode.TOKEN_INVALID
