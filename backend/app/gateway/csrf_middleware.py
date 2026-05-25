"""CSRF 防护中间件 — Double Submit Cookie 模式。

根据 RFC-001 规范，所有状态变更操作都需要 CSRF 防护。

本模块实现了两层 CSRF 防护：

  1. **认证端点来源校验**（is_allowed_auth_origin）：
     登录/注册/初始化等端点免于 Double Submit Token 检查（首次请求无 Token），
     但仍然验证 Origin 头，防止跨站请求伪造。允许的来源包括：
     - 同源请求（浏览器 Origin 与请求目标一致）
     - GATEWAY_CORS_ORIGINS 中配置的显式白名单
     - 无 Origin 头的非浏览器客户端（curl、移动端集成）

  2. **非认证端点 Double Submit Cookie**（should_check_csrf）：
     POST/PUT/DELETE/PATCH 等状态变更请求必须同时携带：
     - Cookie 中的 csrf_token
     - Header 中的 X-CSRF-Token
     两者必须匹配才放行。GET/HEAD/OPTIONS/TRACE 请求免检。

核心设计：
  - 遵循 RFC 7231 安全方法语义，只对状态变更方法做校验
  - Cookie 设置为 SameSite=Strict，JavaScript 可读（httponly=False），
    以便前端读取后放入请求头
  - 使用 secrets.compare_digest 做常量时间比较，防止时序攻击
  - 支持反向代理环境：通过 X-Forwarded-Proto/Host 和 Forwarded 头
    解析原始请求的协议和主机名
  - Origin 归一化处理：只保留 scheme://host[:port]，拒绝包含路径、
    查询参数或用户信息的非法值
"""

import os
import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

# CSRF Cookie 和 Header 的名称
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
# Token 长度（字节），64 字节 = 86 字符的 base64 编码
CSRF_TOKEN_LENGTH = 64  # bytes


def is_secure_request(request: Request) -> bool:
    """检测原始客户端请求是否通过 HTTPS 发出。

    通过检查代理头（Forwarded/X-Forwarded-Proto）判断实际协议，
    而非依赖直接连接的 request.url.scheme。

    Args:
        request: FastAPI 请求对象。

    Returns:
        True 表示请求通过 HTTPS 发出。
    """
    return _request_scheme(request) == "https"


def generate_csrf_token() -> str:
    """生成安全的随机 CSRF 令牌。

    使用 secrets.token_urlsafe 生成密码学安全的随机令牌。

    Returns:
        URL 安全的 Base64 编码令牌字符串。
    """
    return secrets.token_urlsafe(CSRF_TOKEN_LENGTH)


def should_check_csrf(request: Request) -> bool:
    """判断请求是否需要 CSRF 校验。

    根据 RFC 7231，仅对状态变更方法（POST、PUT、DELETE、PATCH）执行 CSRF 校验。
    GET、HEAD、OPTIONS、TRACE 等安全方法免除校验。

    Args:
        request: FastAPI 请求对象。

    Returns:
        True 表示该请求需要 CSRF 校验。
    """
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return False

    path = request.url.path.rstrip("/")
    # 免检 /api/v1/auth/me 端点（读取当前用户信息，非状态变更）
    if path == "/api/v1/auth/me":
        return False
    return True


# 认证端点精确路径集合 — 这些端点免于 Double Submit Token 检查
_AUTH_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/auth/login/local",
        "/api/v1/auth/logout",
        "/api/v1/auth/register",
        "/api/v1/auth/initialize",
    }
)


def is_auth_endpoint(request: Request) -> bool:
    """检查请求是否访问认证端点。

    认证端点首次调用时没有 CSRF Token（因为还没有 Cookie），
    因此免于 Double Submit Token 校验，但仍需来源校验。

    Args:
        request: FastAPI 请求对象。

    Returns:
        True 表示请求访问的是认证端点。
    """
    return request.url.path.rstrip("/") in _AUTH_EXEMPT_PATHS


def _host_with_optional_port(hostname: str, port: int | None, scheme: str) -> str:
    """返回归一化的 host[:port]，省略默认端口。

    Args:
        hostname: 主机名。
        port: 端口号，None 表示未指定。
        scheme: 协议（http/https）。

    Returns:
        归一化的 host[:port] 字符串。
    """
    host = hostname.lower()
    # IPv6 地址需要用方括号包裹
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    # 省略 HTTP 默认的 80 端口和 HTTPS 默认的 443 端口
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return host
    return f"{host}:{port}"


def _normalize_origin(origin: str) -> str | None:
    """将 Origin 字符串归一化为 scheme://host[:port] 格式。

    Args:
        origin: 原始 Origin 头值。

    Returns:
        归一化后的 Origin 字符串，无效输入返回 None。
    """
    try:
        parsed = urlsplit(origin.strip())
        port = parsed.port
    except ValueError:
        return None

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None

    # 浏览器 Origin 只包含 scheme/host/port。
    # 拒绝 URL 形式或包含凭据的值（防御伪造）。
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        return None

    return f"{scheme}://{_host_with_optional_port(parsed.hostname, port, scheme)}"


def _configured_cors_origins() -> set[str]:
    """从环境变量 GATEWAY_CORS_ORIGINS 读取并返回归一化的浏览器来源白名单。

    Returns:
        归一化的 Origin 字符串集合。
    """
    origins = set()
    for raw_origin in os.environ.get("GATEWAY_CORS_ORIGINS", "").split(","):
        origin = raw_origin.strip()
        if not origin or origin == "*":
            continue
        normalized = _normalize_origin(origin)
        if normalized:
            origins.add(normalized)
    return origins


def get_configured_cors_origins() -> set[str]:
    """获取归一化的 CORS 来源白名单（公开接口）。

    Returns:
        归一化的 Origin 字符串集合。
    """
    return _configured_cors_origins()


def _first_header_value(value: str | None) -> str | None:
    """从逗号分隔的代理头中提取第一个值。

    反向代理可能追加多个值，取第一个（最原始的客户端值）。

    Args:
        value: 代理头的原始值。

    Returns:
        第一个非空值，或 None。
    """
    if not value:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def _forwarded_param(request: Request, name: str) -> str | None:
    """从 RFC 7239 Forwarded 头的第一个条目中提取参数。

    Args:
        request: FastAPI 请求对象。
        name: 要提取的参数名（如 "proto"、"host"）。

    Returns:
        参数值，或 None。
    """
    forwarded = _first_header_value(request.headers.get("forwarded"))
    if not forwarded:
        return None

    for part in forwarded.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.lower() == name:
            return value.strip().strip('"') or None
    return None


def _request_scheme(request: Request) -> str:
    """从可信代理头解析原始请求协议。

    解析优先级：Forwarded:proto → X-Forwarded-Proto → request.url.scheme

    Args:
        request: FastAPI 请求对象。

    Returns:
        小写的协议字符串（"http" 或 "https"）。
    """
    scheme = _forwarded_param(request, "proto") or _first_header_value(request.headers.get("x-forwarded-proto")) or request.url.scheme
    return scheme.lower()


def _request_origin(request: Request) -> str | None:
    """构建浏览器目标请求的 Origin（scheme://host[:port]）。

    综合考虑 Forwarded、X-Forwarded-Host、X-Forwarded-Port 等代理头。

    Args:
        request: FastAPI 请求对象。

    Returns:
        归一化的 Origin 字符串，或 None。
    """
    scheme = _request_scheme(request)
    host = _forwarded_param(request, "host") or _first_header_value(request.headers.get("x-forwarded-host")) or request.headers.get("host") or request.url.netloc

    # X-Forwarded-Port 仅在 host 中尚未包含端口时追加
    forwarded_port = _first_header_value(request.headers.get("x-forwarded-port"))
    if forwarded_port and ":" not in host.rsplit("]", 1)[-1]:
        host = f"{host}:{forwarded_port}"

    return _normalize_origin(f"{scheme}://{host}")


def is_allowed_auth_origin(request: Request) -> bool:
    """判断认证端点 POST 请求的来源是否被允许。

    登录/注册/初始化端点免除 Double Submit Token 检查（首次浏览器客户端
    尚无 CSRF Token），但仍会创建会话 Cookie。因此必须拒绝带有恶意 Origin
    头的浏览器请求，防止登录 CSRF / 会话固定攻击。无 Origin 头的请求
    允许通过，以支持 curl 和移动端集成等非浏览器客户端。

    Args:
        request: FastAPI 请求对象。

    Returns:
        True 表示来源被允许。
    """
    origin = request.headers.get("origin")
    if not origin:
        # 非浏览器客户端（无 Origin 头）直接放行
        return True

    normalized_origin = _normalize_origin(origin)
    if normalized_origin is None:
        # 无法归一化的 Origin 视为非法
        return False

    request_origin = _request_origin(request)
    # 允许条件：在 CORS 白名单中 或 与请求目标同源
    return normalized_origin in _configured_cors_origins() or (request_origin is not None and normalized_origin == request_origin)


class CSRFMiddleware(BaseHTTPMiddleware):
    """CSRF 防护中间件，实现 Double Submit Cookie 模式。

    处理流程：
      1. 对认证端点的状态变更请求执行来源校验
      2. 对非认证端点的状态变更请求执行 Double Submit Token 校验
      3. 认证端点的 POST 响应自动设置新的 CSRF Cookie
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        _is_auth = is_auth_endpoint(request)

        # 认证端点的状态变更请求：检查来源是否合法
        if should_check_csrf(request) and _is_auth and not is_allowed_auth_origin(request):
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-site auth request denied."},
            )

        # 非认证端点的状态变更请求：Double Submit Token 校验
        if should_check_csrf(request) and not _is_auth:
            cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
            header_token = request.headers.get(CSRF_HEADER_NAME)

            if not cookie_token or not header_token:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF token missing. Include X-CSRF-Token header."},
                )

            # 使用常量时间比较防止时序攻击
            if not secrets.compare_digest(cookie_token, header_token):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF token mismatch."},
                )

        response = await call_next(request)

        # 认证端点的 POST 响应：生成新的 CSRF Token 并写入 Cookie
        if _is_auth and request.method == "POST":
            csrf_token = generate_csrf_token()
            is_https = is_secure_request(request)
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=csrf_token,
                # Double Submit Cookie 模式要求 JavaScript 可读取
                httponly=False,
                secure=is_https,
                samesite="strict",
            )

        return response


def get_csrf_token(request: Request) -> str | None:
    """从当前请求的 Cookie 中获取 CSRF Token。

    适用于服务端渲染场景，需要将 Token 嵌入表单或请求头。

    Args:
        request: FastAPI 请求对象。

    Returns:
        CSRF Token 字符串，或 None（如果不存在）。
    """
    return request.cookies.get(CSRF_COOKIE_NAME)
