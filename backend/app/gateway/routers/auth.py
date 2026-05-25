"""身份认证（Authentication）端点路由。

本模块实现了完整的用户认证生命周期管理，包括：

核心功能：
- 本地邮箱/密码登录（login）
- 新用户注册（register）
- 系统首次初始化管理员账户（initialize）
- 获取当前用户信息（me）
- 修改密码（change-password），同时支持首次设置流程
- 登出（logout）

安全机制：
- 基于 HttpOnly Cookie 的会话管理，令牌不暴露给 JavaScript
- 基于 IP 的登录速率限制（5 次失败后锁定 5 分钟）
- 弱密码黑名单检查（基于公开的常见密码列表）
- 最小密码长度要求（8 位）
- CSRF 防护（SameSite=Lax）
- 受信代理 IP 提取，防止 X-Forwarded-For 伪造

局限性：
- 速率限制为进程内实现，多 Worker 部署时每个 Worker 独立计数
- OAuth 登录为占位实现，尚未完成

路由前缀：/api/v1/auth
标签：auth
"""

import asyncio
import logging
import os
import time
from ipaddress import ip_address, ip_network

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.gateway.auth import (
    UserResponse,
    create_access_token,
)
from app.gateway.auth.config import get_auth_config
from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse
from app.gateway.csrf_middleware import is_secure_request
from app.gateway.deps import get_current_user_from_request, get_local_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ── 请求/响应模型 ─────────────────────────────────────────────────────────


class LoginResponse(BaseModel):
    """登录响应模型——令牌仅存储在 HttpOnly Cookie 中。

    Attributes:
        expires_in: 令牌有效期（秒）。
        needs_setup: 用户是否需要完成首次设置。
    """

    expires_in: int  # seconds
    needs_setup: bool = False


# 常见密码黑名单。来源于公开的 SecLists "10k worst passwords" 集合，
# 仅保留长度>=8的小写条目（短密码已被 min_length 检查拦截）。
# 这是**最低防线**，非完整的 HIBP/passlib 检查，每次请求进程内执行。
_COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "password",
        "password1",
        "password12",
        "password123",
        "password1234",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty12",
        "qwertyui",
        "qwerty123",
        "abc12345",
        "abcd1234",
        "iloveyou",
        "letmein1",
        "welcome1",
        "welcome123",
        "admin123",
        "administrator",
        "passw0rd",
        "p@ssw0rd",
        "monkey12",
        "trustno1",
        "sunshine",
        "princess",
        "football",
        "baseball",
        "superman",
        "batman123",
        "starwars",
        "dragon123",
        "master123",
        "shadow12",
        "michael1",
        "jennifer",
        "computer",
    }
)


def _password_is_common(password: str) -> bool:
    """大小写不敏感的常见密码黑名单检查。

    将输入转为小写后匹配，以捕获 Password / PASSWORD 等简单变体。
    不对数字替换做归一化（p@ssw0rd 作为字面量条目）——保持规则廉价可预测。

    Args:
        password: 待检查的密码字符串。

    Returns:
        True 如果密码在黑名单中。
    """
    return password.lower() in _COMMON_PASSWORDS


def _validate_strong_password(value: str) -> str:
    """共享的密码强度验证逻辑，供 Register 和 ChangePassword 使用。

    将密码强度规则提升为独立函数而非类型级混入，因为两个请求模型
    没有继承关系，仅共享密码强度规则。通过 field_validator 绑定即可，
    无需继承体操。

    Args:
        value: 待验证的密码字符串。

    Returns:
        验证通过的密码字符串。

    Raises:
        ValueError: 当密码过于常见时抛出。
    """
    if _password_is_common(value):
        raise ValueError("Password is too common; choose a stronger password.")
    return value


class RegisterRequest(BaseModel):
    """用户注册请求模型。

    Attributes:
        email: 用户邮箱地址。
        password: 密码（最少 8 位，需通过强度检查）。
    """

    email: EmailStr
    password: str = Field(..., min_length=8)

    _strong_password = field_validator("password")(classmethod(lambda cls, v: _validate_strong_password(v)))


class ChangePasswordRequest(BaseModel):
    """修改密码请求模型（同时处理首次设置流程）。

    Attributes:
        current_password: 当前密码。
        new_password: 新密码（最少 8 位，需通过强度检查）。
        new_email: 可选的新邮箱地址（用于首次设置时更新邮箱）。
    """

    current_password: str
    new_password: str = Field(..., min_length=8)
    new_email: EmailStr | None = None

    _strong_password = field_validator("new_password")(classmethod(lambda cls, v: _validate_strong_password(v)))


class MessageResponse(BaseModel):
    """通用消息响应模型。

    Attributes:
        message: 响应消息文本。
    """

    message: str


# ── 辅助函数 ───────────────────────────────────────────────────────────────


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    """在响应上设置 HttpOnly 的 access_token Cookie。

    根据 HTTPS 状态自动配置 Secure 和 SameSite 属性。
    非 HTTPS 环境下不设置 max_age，使 Cookie 成为会话级。

    Args:
        response: FastAPI 响应对象。
        token: JWT 令牌字符串。
        request: FastAPI 请求对象（用于判断 HTTPS 状态）。
    """
    config = get_auth_config()
    is_https = is_secure_request(request)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=is_https,
        samesite="lax",
        max_age=config.token_expiry_days * 24 * 3600 if is_https else None,
    )


# ── 速率限制 ──────────────────────────────────────────────────────────────
# 进程内字典实现——不跨 Worker 共享。
#
# **局限性**：多 Worker 部署（如 gunicorn -w N）时，每个 Worker 维护独立
# 的锁定表，攻击者实际上获得 N × _MAX_LOGIN_ATTEMPTS 次猜测机会。
# 生产环境多 Worker 场景应替换为共享存储（Redis、数据库计数器）以实施
# 真正的每 IP 限制。

_MAX_LOGIN_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300  # 5 分钟

# IP → (失败次数, 锁定截止时间戳)
_login_attempts: dict[str, tuple[int, float]] = {}


def _trusted_proxies() -> list:
    """解析 AUTH_TRUSTED_PROXIES 环境变量为 ip_network 对象列表。

    支持逗号分隔的 CIDR 或单 IP 条目。空值/未设置表示无受信代理（直连模式）。
    无效条目被跳过并记录警告日志。实时读取以使环境变量覆盖立即生效，
    同时支持测试中 monkeypatch.setenv 而无需清除模块级缓存。

    Returns:
        ip_network 对象列表，可能为空。
    """
    raw = os.getenv("AUTH_TRUSTED_PROXIES", "").strip()
    if not raw:
        return []
    nets = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            nets.append(ip_network(entry, strict=False))
        except ValueError:
            logger.warning("AUTH_TRUSTED_PROXIES: ignoring invalid entry %r", entry)
    return nets


def _get_client_ip(request: Request) -> str:
    """提取真实客户端 IP 用于速率限制。

    信任模型：
    - TCP 对端（request.client.host）始终作为基线，由内核报告，不可被客户端伪造。
    - X-Real-IP 仅在 TCP 对端位于 AUTH_TRUSTED_PROXIES 白名单中时才被采纳。
      此时假设网关位于反向代理之后，代理会覆盖 X-Real-IP 为原始客户端地址。
    - 无 AUTH_TRUSTED_PROXIES 设置时，X-Real-IP 被静默忽略——防止客户端在
      开发/直连模式下通过伪造头绕过每 IP 速率限制。
    - X-Forwarded-For 被有意忽略，因为它在第一跳处天然可被客户端控制，
      信任链难以逐请求审计。

    Args:
        request: FastAPI 请求对象。

    Returns:
        客户端 IP 地址字符串。
    """
    peer_host = request.client.host if request.client else None

    trusted = _trusted_proxies()
    if trusted and peer_host:
        try:
            peer_ip = ip_address(peer_host)
            if any(peer_ip in net for net in trusted):
                real_ip = request.headers.get("x-real-ip", "").strip()
                if real_ip:
                    return real_ip
        except ValueError:
            # peer_host 不是可解析的 IP（如 "unknown"），回退到基线
            pass

    return peer_host or "unknown"


def _check_rate_limit(ip: str) -> None:
    """检查指定 IP 是否被锁定，若被锁定则抛出 429。

    如果锁定时间已过，自动清除对应记录。

    Args:
        ip: 客户端 IP 地址。

    Raises:
        HTTPException: 状态码 429，当 IP 当前被锁定时抛出。
    """
    record = _login_attempts.get(ip)
    if record is None:
        return
    fail_count, lock_until = record
    if fail_count >= _MAX_LOGIN_ATTEMPTS:
        if time.time() < lock_until:
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Try again later.",
            )
        # 锁定期已过，清除记录
        del _login_attempts[ip]


_MAX_TRACKED_IPS = 10000


def _record_login_failure(ip: str) -> None:
    """记录指定 IP 的登录失败尝试。

    当追踪 IP 总数超过上限时，优先驱逐已过期的锁定记录；
    若仍然超限，则按过期时间排序驱逐最早的一半记录。

    Args:
        ip: 客户端 IP 地址。
    """
    # 字典过大时驱逐已过期条目以控制内存使用
    if len(_login_attempts) >= _MAX_TRACKED_IPS:
        now = time.time()
        expired = [k for k, (c, t) in _login_attempts.items() if c >= _MAX_LOGIN_ATTEMPTS and now >= t]
        for k in expired:
            del _login_attempts[k]
        # 仍然过大时，按截止时间排序驱逐代价最低的一半
        if len(_login_attempts) >= _MAX_TRACKED_IPS:
            by_time = sorted(_login_attempts.items(), key=lambda kv: kv[1][1])
            for k, _ in by_time[: len(by_time) // 2]:
                del _login_attempts[k]

    record = _login_attempts.get(ip)
    if record is None:
        _login_attempts[ip] = (1, 0.0)
    else:
        new_count = record[0] + 1
        # 达到最大失败次数时设置锁定截止时间
        lock_until = time.time() + _LOCKOUT_SECONDS if new_count >= _MAX_LOGIN_ATTEMPTS else 0.0
        _login_attempts[ip] = (new_count, lock_until)


def _record_login_success(ip: str) -> None:
    """登录成功时清除对应 IP 的失败计数器。

    Args:
        ip: 客户端 IP 地址。
    """
    _login_attempts.pop(ip, None)


# ── 端点实现 ───────────────────────────────────────────────────────────────


@router.post("/login/local", response_model=LoginResponse)
async def login_local(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    """本地邮箱/密码登录。

    执行速率限制检查后，验证用户凭据。成功时在 HttpOnly Cookie 中
    设置 JWT 令牌。

    Args:
        request: FastAPI 请求对象。
        response: FastAPI 响应对象（用于设置 Cookie）。
        form_data: OAuth2 标准表单数据（username 为邮箱）。

    Returns:
        LoginResponse，包含令牌有效期和设置状态。

    Raises:
        HTTPException: 状态码 401（凭据错误）或 429（速率限制）。
    """
    client_ip = _get_client_ip(request)
    _check_rate_limit(client_ip)

    user = await get_local_provider().authenticate({"email": form_data.username, "password": form_data.password})

    if user is None:
        _record_login_failure(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AuthErrorResponse(code=AuthErrorCode.INVALID_CREDENTIALS, message="Incorrect email or password").model_dump(),
        )

    _record_login_success(client_ip)
    # 基于用户 ID 和 token 版本创建 JWT
    token = create_access_token(str(user.id), token_version=user.token_version)
    _set_session_cookie(response, token, request)

    return LoginResponse(
        expires_in=get_auth_config().token_expiry_days * 24 * 3600,
        needs_setup=user.needs_setup,
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(request: Request, response: Response, body: RegisterRequest):
    """注册新用户账户（始终为 'user' 角色）。

    首个管理员通过 /initialize 端点显式创建。
    注册成功后自动登录（设置会话 Cookie）。

    Args:
        request: FastAPI 请求对象。
        response: FastAPI 响应对象。
        body: 注册请求体。

    Returns:
        新创建的用户信息。

    Raises:
        HTTPException: 状态码 400（邮箱已注册）。
    """
    try:
        user = await get_local_provider().create_user(email=body.email, password=body.password, system_role="user")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=AuthErrorResponse(code=AuthErrorCode.EMAIL_ALREADY_EXISTS, message="Email already registered").model_dump(),
        )

    # 注册后自动登录
    token = create_access_token(str(user.id), token_version=user.token_version)
    _set_session_cookie(response, token, request)

    return UserResponse(id=str(user.id), email=user.email, system_role=user.system_role)


@router.post("/logout", response_model=MessageResponse)
async def logout(request: Request, response: Response):
    """登出当前用户，清除会话 Cookie。

    Args:
        request: FastAPI 请求对象。
        response: FastAPI 响应对象（用于清除 Cookie）。

    Returns:
        成功登出消息。
    """
    response.delete_cookie(key="access_token", secure=is_secure_request(request), samesite="lax")
    return MessageResponse(message="Successfully logged out")


@router.post("/change-password", response_model=MessageResponse)
async def change_password(request: Request, response: Response, body: ChangePasswordRequest):
    """修改当前认证用户的密码，同时处理首次设置流程。

    行为说明：
    - 若提供 new_email，更新邮箱（检查唯一性）
    - 若用户 needs_setup=True 且提供了 new_email，清除 needs_setup 标志
    - 始终递增 token_version 使旧会话失效
    - 重新签发包含新 token_version 的会话 Cookie

    Args:
        request: FastAPI 请求对象。
        response: FastAPI 响应对象。
        body: 修改密码请求体。

    Returns:
        成功消息。

    Raises:
        HTTPException: 状态码 400（当前密码错误 / 邮箱已被使用 / OAuth 用户）。
    """
    from app.gateway.auth.password import hash_password_async, verify_password_async

    user = await get_current_user_from_request(request)

    # OAuth 用户无密码哈希，不允许修改密码
    if user.password_hash is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrorResponse(code=AuthErrorCode.INVALID_CREDENTIALS, message="OAuth users cannot change password").model_dump())

    if not await verify_password_async(body.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrorResponse(code=AuthErrorCode.INVALID_CREDENTIALS, message="Current password is incorrect").model_dump())

    provider = get_local_provider()

    # 更新邮箱（如果提供）
    if body.new_email is not None:
        existing = await provider.get_user_by_email(body.new_email)
        if existing and str(existing.id) != str(user.id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrorResponse(code=AuthErrorCode.EMAIL_ALREADY_EXISTS, message="Email already in use").model_dump())
        user.email = body.new_email

    # 更新密码 + 递增 token 版本号以使旧会话失效
    user.password_hash = await hash_password_async(body.new_password)
    user.token_version += 1

    # 首次设置流程：提供新邮箱时清除设置标志
    if user.needs_setup and body.new_email is not None:
        user.needs_setup = False

    await provider.update_user(user)

    # 重新签发包含新 token_version 的 Cookie
    token = create_access_token(str(user.id), token_version=user.token_version)
    _set_session_cookie(response, token, request)

    return MessageResponse(message="Password changed successfully")


@router.get("/me", response_model=UserResponse)
async def get_me(request: Request):
    """获取当前认证用户的信息。

    Args:
        request: FastAPI 请求对象。

    Returns:
        当前用户的 ID、邮箱、角色和设置状态。

    Raises:
        HTTPException: 状态码 401（未认证）。
    """
    user = await get_current_user_from_request(request)
    return UserResponse(id=str(user.id), email=user.email, system_role=user.system_role, needs_setup=user.needs_setup)


# 每 IP 缓存：ip → (时间戳, 结果字典)。
# 在 TTL 内返回缓存结果而非 429，因为答案（管理员是否存在）很少变化，
# 返回 429 会导致多标签页/重启后重连风暴。
_SETUP_STATUS_CACHE: dict[str, tuple[float, dict]] = {}
_SETUP_STATUS_CACHE_TTL_SECONDS = 60
_MAX_TRACKED_SETUP_STATUS_IPS = 10000
_SETUP_STATUS_INFLIGHT: dict[str, asyncio.Task[dict]] = {}
_SETUP_STATUS_INFLIGHT_GUARD = asyncio.Lock()


@router.get("/setup-status")
async def setup_status(request: Request):
    """检查系统是否已完成初始设置（是否存在管理员账户）。

    当无管理员账户时返回 needs_setup=True。使用每 IP 缓存和去重机制
    防止重复计算和重连风暴。

    Args:
        request: FastAPI 请求对象。

    Returns:
        包含 needs_setup 布尔值的字典。
    """
    client_ip = _get_client_ip(request)
    now = time.time()

    # TTL 内返回缓存结果——避免多标签页重连时触发 429
    cached = _SETUP_STATUS_CACHE.get(client_ip)
    if cached is not None:
        cached_time, cached_result = cached
        if now - cached_time < _SETUP_STATUS_CACHE_TTL_SECONDS:
            return cached_result

    async with _SETUP_STATUS_INFLIGHT_GUARD:
        # 等待去重锁后重新检查缓存
        now = time.time()
        cached = _SETUP_STATUS_CACHE.get(client_ip)
        if cached is not None:
            cached_time, cached_result = cached
            if now - cached_time < _SETUP_STATUS_CACHE_TTL_SECONDS:
                return cached_result

        task = _SETUP_STATUS_INFLIGHT.get(client_ip)
        if task is None:
            # 字典过大时驱逐过期条目以控制内存
            if len(_SETUP_STATUS_CACHE) >= _MAX_TRACKED_SETUP_STATUS_IPS:
                cutoff = now - _SETUP_STATUS_CACHE_TTL_SECONDS
                stale = [k for k, (t, _) in _SETUP_STATUS_CACHE.items() if t < cutoff]
                for k in stale:
                    del _SETUP_STATUS_CACHE[k]
                if len(_SETUP_STATUS_CACHE) >= _MAX_TRACKED_SETUP_STATUS_IPS:
                    by_time = sorted(_SETUP_STATUS_CACHE.items(), key=lambda entry: entry[1][0])
                    for k, _ in by_time[: len(by_time) // 2]:
                        del _SETUP_STATUS_CACHE[k]

            async def _compute_setup_status() -> dict:
                admin_count = await get_local_provider().count_admin_users()
                return {"needs_setup": admin_count == 0}

            task = asyncio.create_task(_compute_setup_status())
            _SETUP_STATUS_INFLIGHT[client_ip] = task

    try:
        result = await task
    finally:
        async with _SETUP_STATUS_INFLIGHT_GUARD:
            if _SETUP_STATUS_INFLIGHT.get(client_ip) is task:
                del _SETUP_STATUS_INFLIGHT[client_ip]

    # 仅缓存稳定的"已初始化"结果，避免陈旧的设置重定向
    if result["needs_setup"] is False:
        _SETUP_STATUS_CACHE[client_ip] = (time.time(), result)
    else:
        _SETUP_STATUS_CACHE.pop(client_ip, None)
    return result


class InitializeAdminRequest(BaseModel):
    """首次启动管理员账户创建请求模型。

    Attributes:
        email: 管理员邮箱地址。
        password: 管理员密码（最少 8 位，需通过强度检查）。
    """

    email: EmailStr
    password: str = Field(..., min_length=8)

    _strong_password = field_validator("password")(classmethod(lambda cls, v: _validate_strong_password(v)))


@router.post("/initialize", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def initialize_admin(request: Request, response: Response, body: InitializeAdminRequest):
    """创建系统首个管理员账户（仅当无管理员存在时可调用）。

    成功后创建 needs_setup=False 的管理员账户并设置会话 Cookie。
    处理并发竞争：若另一请求先完成创建，返回 409 Conflict。

    Args:
        request: FastAPI 请求对象。
        response: FastAPI 响应对象。
        body: 管理员初始化请求体。

    Returns:
        新创建的管理员用户信息。

    Raises:
        HTTPException: 状态码 409（系统已初始化）。
    """
    admin_count = await get_local_provider().count_admin_users()
    if admin_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=AuthErrorResponse(code=AuthErrorCode.SYSTEM_ALREADY_INITIALIZED, message="System already initialized").model_dump(),
        )

    try:
        user = await get_local_provider().create_user(email=body.email, password=body.password, system_role="admin", needs_setup=False)
    except ValueError:
        # DB 唯一约束竞争：另一个并发请求抢先完成了创建
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=AuthErrorResponse(code=AuthErrorCode.SYSTEM_ALREADY_INITIALIZED, message="System already initialized").model_dump(),
        )

    token = create_access_token(str(user.id), token_version=user.token_version)
    _set_session_cookie(response, token, request)

    return UserResponse(id=str(user.id), email=user.email, system_role=user.system_role)


# ── OAuth 端点（未来/占位实现） ────────────────────────────────────────────


@router.get("/oauth/{provider}")
async def oauth_login(provider: str):
    """发起 OAuth 登录流程。

    重定向到 OAuth 提供方的授权 URL。
    目前为占位实现——需要 OAuth 提供方实现。

    Args:
        provider: OAuth 提供方名称（如 "github"、"google"）。

    Raises:
        HTTPException: 状态码 400（不支持的提供方）或 501（未实现）。
    """
    if provider not in ["github", "google"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported OAuth provider: {provider}",
        )

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="OAuth login not yet implemented",
    )


@router.get("/callback/{provider}")
async def oauth_callback(provider: str, code: str, state: str):
    """OAuth 回调端点。

    处理 OAuth 提供方在用户授权后的回调。
    目前为占位实现。

    Args:
        provider: OAuth 提供方名称。
        code: 授权码。
        state: CSRF 防护状态参数。

    Raises:
        HTTPException: 状态码 501（未实现）。
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="OAuth callback not yet implemented",
    )
