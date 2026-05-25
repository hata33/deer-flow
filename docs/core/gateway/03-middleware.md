# 中间件链详解

## 概述

DeerFlow Gateway 的中间件栈由三个层次组成，按 FastAPI 中间件的洋葱模型依次包裹：

```
请求进入方向 →→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→→

┌─────────────────────────────────────────────────────────────────┐
│                    AuthMiddleware（最外层）                       │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                CSRFMiddleware（中间层）                     │  │
│  │  ┌───────────────────────────────────────────────────────┐│  │
│  │  │             CORSMiddleware（最内层）                    ││  │
│  │  │  ┌─────────────────────────────────────────────────┐  ││  │
│  │  │  │              路由处理函数                        │  ││  │
│  │  │  └─────────────────────────────────────────────────┘  ││  │
│  │  └───────────────────────────────────────────────────────┘│  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘

←←←←←←←←←←←←←←←←←←←←← 响应返回方向 ←←←←←←←←←←←←←←←←←←←←←←←←
```

**添加顺序**（`create_app()` 中）：
```python
app.add_middleware(AuthMiddleware)    # 第 1 个添加
app.add_middleware(CSRFMiddleware)    # 第 2 个添加
app.add_middleware(CORSMiddleware, …) # 第 3 个添加
```

**执行顺序**：请求进入时 CORS → CSRF → Auth → Router；响应返回时逆序。

## CORS 中间件

**类**：`starlette.middleware.cors.CORSMiddleware`

### 配置

| 参数 | 来源 | 说明 |
|------|------|------|
| `allow_origins` | `GATEWAY_CORS_ORIGINS` 环境变量 | 允许的源列表（逗号分隔） |
| `allow_credentials` | 固定 `True` | 允许携带 Cookie |
| `allow_methods` | 固定 `["*"]` | 允许所有 HTTP 方法 |
| `allow_headers` | 固定 `["*"]` | 允许所有请求头 |

### 同源默认行为

当请求通过 Nginx（端口 2026）进入时，前端和 API 位于同一源，CORS 默认不拦截。只有在以下场景需要显式配置 `GATEWAY_CORS_ORIGINS`：

- 直接访问 Gateway 端口 8001（跨端口）
- 分离部署的浏览器客户端
- 开发环境中使用不同端口

### 源规范化

`CSRFMiddleware._normalize_origin()` 对 `GATEWAY_CORS_ORIGINS` 中的每个源执行规范化：

1. 解析 `scheme://host[:port]`
2. 小写化 scheme 和 host
3. 省略默认端口（HTTP 80、HTTPS 443）
4. 拒绝包含用户名、密码、路径、查询、片段的值
5. 跳过 `*` 通配符（不允许通配源）

### CORS 与 CSRF 共享源配置

`GATEWAY_CORS_ORIGINS` 同时被 CORS 中间件和 CSRF 中间件使用，确保两者的源检查始终一致。这是通过 `get_configured_cors_origins()` 函数实现的：

```python
# csrf_middleware.py
def get_configured_cors_origins() -> set[str]:
    return _configured_cors_origins()

# app.py
cors_origins = sorted(get_configured_cors_origins())
app.add_middleware(CORSMiddleware, allow_origins=cors_origins, …)
```

## CSRF 中间件

**类**：`CSRFMiddleware`（`csrf_middleware.py`）

### Double Submit Cookie 模式

CSRF 防护采用标准的双重提交 Cookie 模式：

1. 服务端通过 Set-Cookie 响应头向浏览器写入随机 CSRF Token
2. 前端 JavaScript 读取 Cookie 值，在每个状态变更请求中通过 `X-CSRF-Token` 头部发送
3. 服务端比对 Cookie 值和 Header 值是否一致

```
首次登录/注册：
  ┌─ 服务器 ──────────────────────────────────┐
  │  POST /api/v1/auth/login/local             │
  │  → 验证凭据                                │
  │  → 设置 access_token Cookie（HttpOnly）     │
  │  → 设置 csrf_token Cookie（JS 可读）        │
  └────────────────────────────────────────────┘

后续状态变更请求：
  ┌─ 浏览器 ──────────────────────────────────────────────┐
  │  POST /api/threads/xxx/runs/stream                     │
  │  Cookie: access_token=eyJ…; csrf_token=abc123         │
  │  Header: X-CSRF-Token: abc123                         │
  └───────────────────────────────────────────────────────┘
  ┌─ 服务器 ──────────────────────────────────────────────┐
  │  比对 Cookie("abc123") == Header("abc123")             │
  │  → 匹配 → 放行                                        │
  │  → 不匹配 → 403 CSRF token mismatch                   │
  └───────────────────────────────────────────────────────┘
```

### CSRF 检查逻辑

```python
def should_check_csrf(request: Request) -> bool:
    # 仅状态变更方法需要 CSRF 验证
    return request.method in ("POST", "PUT", "DELETE", "PATCH")
```

### 路径分类处理

| 路径类型 | CSRF 检查 | Origin 检查 | 说明 |
|----------|-----------|-------------|------|
| 认证端点（login/register/initialize/logout） | 否 | 是 | 首次请求无 CSRF Token |
| `/api/v1/auth/me`（POST） | 否 | 否 | 特殊豁免 |
| 其他状态变更端点 | 是（Cookie + Header） | 否 | 标准 CSRF 验证 |
| 安全方法（GET/HEAD/OPTIONS） | 否 | 否 | 无副作用 |

### CSRF Token 生成

```python
def generate_csrf_token() -> str:
    return secrets.token_urlsafe(64)  # 64 字节 = 86 字符
```

### HTTPS 检测

`is_secure_request()` 通过以下顺序判断原始请求是否为 HTTPS：

1. `Forwarded` 头部的 `proto` 参数（RFC 7239）
2. `X-Forwarded-Proto` 头部
3. `request.url.scheme`

这确保在 Nginx 反向代理后端仍能正确判断 HTTPS。

### CSRF Cookie 属性

| 属性 | 值 | 说明 |
|------|-----|------|
| `httponly` | `False` | JavaScript 必须能读取（Double Submit Cookie） |
| `secure` | 取决于 HTTPS | HTTPS 环境下启用 |
| `samesite` | `"strict"` | 严格同站策略 |

## Auth 中间件

**类**：`AuthMiddleware`（`auth_middleware.py`）

### 定位：Fail-Closed 安全网

Auth 中间件是认证的**最终保障**。即使路由层缺少 `@require_auth` 装饰器，非白名单路径的未认证请求也会被拒绝。

### 白名单路径

**前缀匹配**（路径以指定前缀开始即放行）：

| 前缀 | 说明 |
|------|------|
| `/health` | 健康检查 |
| `/docs` | Swagger UI |
| `/redoc` | ReDoc 文档 |
| `/openapi.json` | OpenAPI Schema |

**精确匹配**（路径完全一致才放行，尾部斜杠自动去除）：

| 路径 | 说明 |
|------|------|
| `/api/v1/auth/login/local` | 登录 |
| `/api/v1/auth/register` | 注册 |
| `/api/v1/auth/logout` | 登出 |
| `/api/v1/auth/setup-status` | 初始化状态 |
| `/api/v1/auth/initialize` | 创建管理员 |

### 内部认证路径

当请求携带有效的 `X-DeerFlow-Internal-Token` 头部时，使用合成内部用户跳过 JWT 验证。这用于 Channel Worker 的进程内通信。

### 两阶段验证

```
阶段 1：Cookie 存在性检查
  └── access_token Cookie 缺失 → 401 NOT_AUTHENTICATED

阶段 2：JWT 严格验证
  ├── 解码 JWT → TokenError → 401（TOKEN_EXPIRED/TOKEN_INVALID）
  ├── 数据库查找用户 → 不存在 → 401 USER_NOT_FOUND
  └── token_version 不匹配 → 401 TOKEN_INVALID
```

### 用户上下文注入

验证成功后，中间件同时设置两个上下文：

| 位置 | 用途 |
|------|------|
| `request.state.user` | 路由层通过 `request.state.user` 访问 |
| `request.state.auth` | `@require_permission` 快速短路，避免重复 JWT 解码 |
| `user_context` contextvar | 仓储层自动所有者过滤 |

### ContextVar 生命周期

```python
token = set_current_user(user)
try:
    return await call_next(request)
finally:
    reset_current_user(token)
```

`finally` 块确保即使后续处理抛出异常，contextvar 也会被正确重置，避免用户信息泄漏到其他请求。

## LangGraph 认证集成

**文件**：`langgraph_auth.py`

### 适用场景

此模块用于通过 `langgraph.json` 的 `auth.path` 直接启动 LangGraph Server 的场景（非默认的嵌入式 Gateway 模式）。

### 两层结构

```python
auth = Auth()

@auth.authenticate
async def authenticate(request):
    """第 1 层：JWT Cookie 验证 + CSRF 检查"""
    _check_csrf(request)           # CSRF 验证
    token = request.cookies.get("access_token")
    payload = decode_token(token)  # JWT 解码
    user = await get_local_provider().get_user(payload.sub)
    # token_version 一致性检查
    return payload.sub

@auth.on
async def add_owner_filter(ctx, value):
    """第 2 层：自动注入 user_id 过滤"""
    value["metadata"]["user_id"] = ctx.user.identity
    return {"user_id": ctx.user.identity}
```

### CSRF 镜像逻辑

`_check_csrf()` 函数完全镜像 Gateway `CSRFMiddleware` 的验证逻辑：

```python
def _check_csrf(request) -> None:
    # 仅检查状态变更方法
    if method.upper() not in {"POST", "PUT", "DELETE", "PATCH"}:
        return
    # Double Submit Cookie 验证
    cookie_token = request.cookies.get("csrf_token")
    header_token = request.headers.get("x-csrf-token")
    if not cookie_token or not header_token:
        raise 403
    if not secrets.compare_digest(cookie_token, header_token):
        raise 403
```

这确保无论通过 Gateway 还是直接通过 LangGraph Server 访问，CSRF 防护策略完全一致。

## 中间件执行顺序与优先级

### 请求处理完整链路

```
HTTP 请求到达
  │
  ▼ AuthMiddleware.dispatch()
  │  ├── 路径白名单检查 → 放行
  │  ├── 内部 Token 检查 → 合成用户
  │  ├── Cookie 存在性检查
  │  ├── JWT 验证 + 用户查找 + 版本检查
  │  └── 注入 request.state.user + contextvar
  │
  ▼ CSRFMiddleware.dispatch()
  │  ├── 方法检查（仅 POST/PUT/DELETE/PATCH）
  │  ├── 认证端点 → Origin 检查
  │  └── 其他端点 → Cookie + Header 双重验证
  │
  ▼ CORSMiddleware（Starlette 内置）
  │  ├── 预检请求（OPTIONS）→ 直接响应
  │  └── 实际请求 → 添加 CORS 头
  │
  ▼ 路由处理函数
  │  ├── @require_permission → 细粒度权限检查
  │  ├── 业务逻辑
  │  └── 返回响应
  │
  ▼ 响应通过中间件逆序返回
  │  ├── CORSMiddleware → 添加 CORS 头
  │  ├── CSRFMiddleware → 认证端点 POST 设置 CSRF Cookie
  │  └── AuthMiddleware → finally 块重置 contextvar
  │
  ▼ HTTP 响应发送
```

### 优先级总结

| 优先级 | 检查项 | 失败响应 |
|--------|--------|----------|
| 1（最高） | Auth 白名单路径 | 跳过所有检查 |
| 2 | 内部认证 Token | 跳过 JWT 验证 |
| 3 | JWT Cookie 存在性 | 401 NOT_AUTHENTICATED |
| 4 | JWT 有效性 | 401 TOKEN_EXPIRED/INVALID |
| 5 | 用户存在性 | 401 USER_NOT_FOUND |
| 6 | token_version 一致性 | 401 TOKEN_INVALID |
| 7 | CSRF Token 存在性 | 403 CSRF token missing |
| 8 | CSRF Token 一致性 | 403 CSRF token mismatch |
| 9 | CORS Origin | CORS 拦截 |
| 10 | @require_permission | 403 Permission denied |
| 11 | owner_check 所有权 | 404 Thread not found |
