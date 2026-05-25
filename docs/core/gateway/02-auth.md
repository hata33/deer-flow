# 认证授权系统详解

## 架构总览

DeerFlow 采用基于 JWT 的会话认证系统，核心组件分布在 `app/gateway/auth/` 子目录中：

```
认证请求流程：
  Cookie: access_token (JWT)
     │
     ▼
  AuthMiddleware（全局拦截）
     │  验证 Cookie 存在
     │  JWT 解码 + 验证
     │  数据库查找用户
     │  token_version 一致性检查
     ▼
  request.state.user（用户对象注入）
  user_context contextvar（上下文变量设置）
     │
     ▼
  @require_permission（细粒度权限检查）
     │  检查资源权限
     │  执行所有权校验
     ▼
  路由处理函数
```

## AuthConfig：JWT 密钥管理

**文件**：`auth/config.py`

### 密钥来源优先级

```
1. 环境变量 AUTH_JWT_SECRET（生产环境推荐）
2. 文件 .deer-flow/.jwt_secret（自动生成并持久化）
3. 运行时生成（不持久化，重启后失效）
```

### 自动生成与持久化

当 `AUTH_JWT_SECRET` 未设置时，`_load_or_create_secret()` 函数：

1. 尝试读取 `{base_dir}/.jwt_secret` 文件
2. 文件存在且非空则直接使用
3. 否则使用 `secrets.token_urlsafe(32)` 生成新密钥
4. 以 `0o600` 权限写入文件（仅文件所有者可读写）
5. 设置到环境变量以供后续使用

### 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `jwt_secret` | — | JWT 签名密钥（必填） |
| `token_expiry_days` | 7 | Token 有效期（1-30 天） |
| `oauth_github_client_id` | None | GitHub OAuth 客户端 ID |
| `oauth_github_client_secret` | None | GitHub OAuth 密钥 |

## JWT：令牌创建与验证

**文件**：`auth/jwt.py`

使用 `PyJWT` 库，算法为 `HS256`。

### Token 载荷结构

```python
{
    "sub": "<user_uuid>",     # 用户 ID
    "exp": "<expiry_time>",   # 过期时间
    "iat": "<issued_at>",     # 签发时间
    "ver": <token_version>    # 用户 token 版本号
}
```

### `ver` 字段与 Token 失效机制

`ver`（token_version）是实现密码修改后旧会话全部失效的关键机制：

1. 用户创建时 `token_version = 0`
2. 每次修改密码时 `token_version += 1`
3. JWT 签发时将当前 `token_version` 写入 `ver` 字段
4. 验证时比对 JWT 中的 `ver` 与数据库中的 `token_version`
5. 不一致则返回 401（`TOKEN_INVALID`：Token revoked (password changed)）

### 解码错误类型

| TokenError 枚举 | 触发条件 | 映射 AuthErrorCode |
|-----------------|----------|-------------------|
| `EXPIRED` | `jwt.ExpiredSignatureError` | `TOKEN_EXPIRED` |
| `INVALID_SIGNATURE` | `jwt.InvalidSignatureError` | `TOKEN_INVALID` |
| `MALFORMED` | 其他 `jwt.PyJWTError` | `TOKEN_INVALID` |

## 密码：版本化哈希

**文件**：`auth/password.py`

### 哈希格式

```
$dfv<N>$<bcrypt_hash>
```

### 版本演进

| 版本 | 格式 | 算法 | 说明 |
|------|------|------|------|
| **v1**（遗留） | `$dfv1$<bcrypt>` | `bcrypt(password)` | 纯 bcrypt，受 72 字节截断限制 |
| **v2**（当前） | `$dfv2$<bcrypt>` | `bcrypt(SHA-256(password))` | SHA-256 预哈希绕过 72 字节限制 |

### v2 预哈希流程

```python
def _pre_hash_v2(password: str) -> bytes:
    # 1. UTF-8 编码
    # 2. SHA-256 哈希（固定 32 字节输出）
    # 3. Base64 编码
    return base64.b64encode(
        hashlib.sha256(password.encode("utf-8")).digest()
    )
```

### 自动升级机制

```python
def needs_rehash(hashed_password: str) -> bool:
    """检测是否使用旧版本哈希，需要重新哈希。"""
    return not hashed_password.startswith("$dfv2$")
```

登录成功后，如果检测到旧版本哈希，`LocalAuthProvider.authenticate()` 会透明地升级哈希：

```python
if needs_rehash(user.password_hash):
    user.password_hash = await hash_password_async(password)
    await self._repo.update_user(user)
```

升级失败不影响登录成功（best-effort 语义）。

### 弱密码检测

注册和修改密码接口内置常见密码黑名单（约 40 个条目），大小写不敏感。包含如 `password123`、`admin123`、`qwerty123` 等常见弱密码。

## AuthProvider：认证提供者抽象

**文件**：`auth/providers.py` + `auth/local_provider.py`

### 抽象接口

```python
class AuthProvider(ABC):
    async def authenticate(self, credentials: dict) -> User | None
    async def get_user(self, user_id: str) -> User | None
```

### LocalAuthProvider 实现

基于本地数据库的邮箱/密码认证：

| 方法 | 说明 |
|------|------|
| `authenticate(credentials)` | 验证邮箱+密码，自动升级哈希 |
| `get_user(user_id)` | 按 ID 查找用户 |
| `create_user(email, password, ...)` | 创建用户（密码自动哈希） |
| `count_admin_users()` | 统计管理员数量 |
| `update_user(user)` | 更新用户信息 |
| `get_user_by_email(email)` | 按邮箱查找用户 |

### 认证流程

```
authenticate({"email": "...", "password": "..."})
  │
  ├── 1. 根据邮箱查找用户
  ├── 2. 检查 password_hash 是否存在（OAuth 用户无本地密码）
  ├── 3. 验证密码：verify_password_async(password, hash)
  │      ├── v2 哈希：SHA-256(password) → bcrypt 验证
  │      └── v1/裸 bcrypt：直接 bcrypt 验证
  ├── 4. 检查是否需要哈希升级 → 自动重哈希
  └── 5. 返回 User 对象
```

## UserRepository：用户存储抽象

**文件**：`auth/repositories/base.py` + `auth/repositories/sqlite.py`

### 抽象接口

```python
class UserRepository(ABC):
    async def create_user(user: User) -> User          # 创建用户
    async def get_user_by_id(user_id: str) -> User|None # 按 ID 查找
    async def get_user_by_email(email: str) -> User|None# 按邮箱查找
    async def update_user(user: User) -> User           # 更新用户
    async def count_users() -> int                       # 用户总数
    async def count_admin_users() -> int                 # 管理员数量
    async def get_user_by_oauth(provider, oauth_id) -> User|None
```

### SQLite 实现

使用共享的 SQLAlchemy 异步引擎，与 `threads_meta`、`runs`、`run_events`、`feedback` 表共用同一数据库。

**关键设计**：
- `create_user()` 通过 `IntegrityError` 检测邮箱唯一约束冲突
- `update_user()` 在目标行不存在时抛出 `UserNotFoundError`（硬失败，而非静默成功）
- User ↔ UserRow 双向转换处理 SQLite 时区信息丢失问题

### User 模型

```python
class User(BaseModel):
    id: UUID                    # 主键（自动生成）
    email: EmailStr             # 唯一邮箱
    password_hash: str | None   # bcrypt 哈希（OAuth 用户为 None）
    system_role: "admin" | "user"
    created_at: datetime        # 创建时间（UTC）
    oauth_provider: str | None  # OAuth 提供者
    oauth_id: str | None        # OAuth 用户 ID
    needs_setup: bool           # 是否需要完成初始设置
    token_version: int          # 密码修改时递增，使旧 Token 失效
```

## AuthMiddleware：全局认证中间件

**文件**：`auth_middleware.py`

### 白名单路径

以下路径无需认证：

| 类别 | 路径 |
|------|------|
| 前缀匹配 | `/health`、`/docs`、`/redoc`、`/openapi.json` |
| 精确匹配 | `/api/v1/auth/login/local` |
| 精确匹配 | `/api/v1/auth/register` |
| 精确匹配 | `/api/v1/auth/logout` |
| 精确匹配 | `/api/v1/auth/setup-status` |
| 精确匹配 | `/api/v1/auth/initialize` |

注意：`/api/v1/auth/me`、`/api/v1/auth/change-password` 等路径**不在**白名单中。

### 认证流程

```
AuthMiddleware.dispatch(request, call_next)
  │
  ├── 1. 检查路径是否在白名单 → 直接放行
  │
  ├── 2. 检查内部认证 Token（X-DeerFlow-Internal-Token）
  │      └── 有效的内部 Token → 使用合成内部用户
  │
  ├── 3. 检查 Cookie（access_token）
  │      └── 缺少 → 401 NOT_AUTHENTICATED
  │
  ├── 4. JWT 严格验证
  │      ├── 解码 JWT
  │      ├── 数据库查找用户
  │      └── token_version 比对
  │      └── 任何失败 → 401（TOKEN_INVALID/USER_NOT_FOUND 等）
  │
  ├── 5. 注入用户上下文
  │      ├── request.state.user = user
  │      ├── request.state.auth = AuthContext(user, permissions)
  │      └── set_current_user(user) → contextvar
  │
  └── 6. 调用后续处理 → finally 块重置 contextvar
```

### 上下文变量模式

中间件通过 `set_current_user(user)` 将用户信息存入 contextvar，使得仓储层的所有者过滤可以自动生效，无需每个路由手动传递 `user_id`。

## CSRFMiddleware：跨站请求伪造防护

**文件**：`csrf_middleware.py`

### Double Submit Cookie 模式

```
浏览器请求：
  Cookie: csrf_token=<random_token>
  Header: X-CSRF-Token: <same_random_token>
     │
     ▼
  CSRFMiddleware 验证两者一致
```

### 规则

| 条件 | 行为 |
|------|------|
| GET/HEAD/OPTIONS/TRACE | 不检查 CSRF |
| POST/PUT/DELETE/PATCH 到认证端点 | 检查 Origin 是否合法 |
| POST/PUT/DELETE/PATCH 到其他端点 | 检查 Cookie + Header CSRF Token |
| `/api/v1/auth/me`（POST） | 豁免 CSRF 检查 |

### 认证端点 CSRF 处理

认证端点（login/register/logout/initialize）无需 CSRF Token（首次请求时还没有），但会检查 `Origin` 头：

1. 无 `Origin` 头 → 允许（兼容 curl 等非浏览器客户端）
2. `Origin` 匹配请求目标或 `GATEWAY_CORS_ORIGINS` → 允许
3. `Origin` 不匹配 → 403（Cross-site auth request denied）

### CSRF Token 生成

认证端点成功响应（POST）时自动生成并设置 CSRF Cookie：

```python
response.set_cookie(
    key="csrf_token",
    value=secrets.token_urlsafe(64),
    httponly=False,    # 必须可被 JavaScript 读取
    secure=is_https,
    samesite="strict",
)
```

## 内部认证：进程内通信

**文件**：`internal_auth.py`

Channel Worker 运行在 Gateway 同一进程中，通过进程内 Token 认证：

```python
# 进程启动时生成（不持久化，不跨进程共享）
_INTERNAL_AUTH_TOKEN = secrets.token_urlsafe(32)

# 认证头
INTERNAL_AUTH_HEADER_NAME = "X-DeerFlow-Internal-Token"
```

### 内部用户

内部认证成功后使用合成的用户对象：

```python
SimpleNamespace(id=DEFAULT_USER_ID, system_role="internal")
```

### 使用场景

IM 通道的 Channel Manager 通过 `langgraph-sdk` HTTP 客户端与 Gateway 通信时，注入内部认证 Token + CSRF Cookie/Header 对，使 Gateway 接受来自 Channel Worker 的状态变更请求。

## 管理员创建流程

### 首次启动

```
1. 应用启动 → lifespan()
2. _ensure_admin_user(app)
3. admin_count = 0（首次启动无管理员）
4. 日志输出：
   ============================================================
     First boot detected — no admin account exists.
     Visit /setup to complete admin account creation.
   ============================================================
5. 前端检测 /api/v1/auth/setup-status → {"needs_setup": true}
6. 用户访问 /setup 页面
7. 前端调用 POST /api/v1/auth/initialize
8. 创建管理员（needs_setup=false）
9. 自动登录，设置 Cookie
```

### 后续启动

```
1. 应用启动 → _ensure_admin_user(app)
2. admin_count > 0
3. 执行孤立线程迁移：将无 user_id 的 LangGraph 线程分配给管理员
4. 服务正常就绪
```

## 凭据文件管理

**文件**：`auth/credential_file.py`

管理员初始凭据写入 `{base_dir}/admin_initial_credentials.txt`，权限 `0o600`（仅文件所有者可读写）。

**原子性保证**：使用 `os.open()` 配合 `O_WRONLY | O_CREAT | O_TRUNC` 和 mode 参数，确保文件从创建瞬间起就具有正确的权限，不存在 `write_text` + `chmod` 之间的权限窗口。

**内容格式**：

```
# DeerFlow admin initial credentials
# ...
email: admin@example.com
password: <generated_password>
```

## 密码重置 CLI 工具

**文件**：`auth/reset_admin.py`

### 使用方式

```bash
# 重置第一个管理员的密码
python -m app.gateway.auth.reset_admin

# 重置指定邮箱用户的密码
python -m app.gateway.auth.reset_admin --email admin@example.com
```

### 执行流程

```
1. 加载配置 → 初始化数据库引擎
2. 查找目标用户（按邮箱或首个管理员）
3. 生成随机密码：secrets.token_urlsafe(16)
4. 更新密码哈希
5. 递增 token_version（使所有现有会话失效）
6. 设置 needs_setup=true（下次登录需修改密码）
7. 写入凭据文件
8. 输出凭据文件路径（不输出密码本身）
```

## 错误码体系

**文件**：`auth/errors.py`

### AuthErrorCode

| 错误码 | 值 | 含义 |
|--------|-----|------|
| `INVALID_CREDENTIALS` | `invalid_credentials` | 邮箱或密码错误 |
| `TOKEN_EXPIRED` | `token_expired` | JWT 已过期 |
| `TOKEN_INVALID` | `token_invalid` | JWT 无效（签名错误/版本不匹配） |
| `USER_NOT_FOUND` | `user_not_found` | 用户不存在 |
| `EMAIL_ALREADY_EXISTS` | `email_already_exists` | 邮箱已被注册 |
| `PROVIDER_NOT_FOUND` | `provider_not_found` | 认证提供者不存在 |
| `NOT_AUTHENTICATED` | `not_authenticated` | 未认证 |
| `SYSTEM_ALREADY_INITIALIZED` | `system_already_initialized` | 系统已初始化 |

### TokenError

| 枚举 | 值 | 含义 |
|------|-----|------|
| `EXPIRED` | `expired` | JWT 过期 |
| `INVALID_SIGNATURE` | `invalid_signature` | 签名无效 |
| `MALFORMED` | `malformed` | JWT 格式错误 |

### 错误响应格式

```json
{
  "detail": {
    "code": "invalid_credentials",
    "message": "Incorrect email or password"
  }
}
```

## 登录速率限制

**文件**：`routers/auth.py`

| 参数 | 值 | 说明 |
|------|-----|------|
| 最大尝试次数 | 5 | 每个 IP |
| 锁定时间 | 300 秒（5 分钟） | 达到上限后 |
| 最大跟踪 IP | 10000 | 内存保护 |
| 缓存清理 | LRU | 超限时优先清理已过期或低优先级记录 |

### IP 解析策略

```
1. 默认：使用 TCP 对端地址（request.client.host）
2. AUTH_TRUSTED_PROXIES 已配置且对端在信任列表中：
   → 使用 X-Real-IP 头部值
3. 不使用 X-Forwarded-For（客户端可控，信任链难以审计）
```
