# OAuth — 令牌管理

## 模块路径

`deerflow.mcp.oauth`

## 解决的问题

部分 MCP 服务器（如企业 API 网关、受保护的第三方服务）要求客户端在每次请求中携带 OAuth Bearer Token。手动管理令牌生命周期（获取、缓存、过期刷新）非常繁琐。

本模块提供全自动的 OAuth 令牌管理：
- **获取**: 根据配置自动向令牌端点请求令牌
- **缓存**: 内存缓存，避免每次调用都重新获取
- **刷新**: 在令牌过期前自动刷新（提前 `refresh_skew_seconds` 秒）
- **注入**: 通过拦截器在每次工具调用时自动注入 Authorization 头

## 组件结构

```
oauth.py
├── _OAuthToken                  — 令牌数据类（access_token, token_type, expires_at）
├── OAuthTokenManager            — 令牌管理器（获取/缓存/刷新/并发控制）
├── build_oauth_tool_interceptor — 构建工具拦截器（每次调用注入认证头）
└── get_initial_oauth_headers    — 获取初始认证头（连接建立阶段）
```

## OAuthTokenManager — 令牌管理器

### 初始化

通过 `from_extensions_config(extensions_config)` 工厂方法构建。只收集配置了 `oauth.enabled=true` 的服务器。

每个服务器维护独立的：
- `McpOAuthConfig` — OAuth 配置
- `_OAuthToken` — 缓存的令牌
- `asyncio.Lock` — 防止并发刷新

### 令牌获取流程

```
get_authorization_header(server_name)
    │
    ├── 快速路径：缓存命中且未过期
    │   └── return "{token_type} {access_token}"
    │
    └── 慢速路径：需要获取/刷新
        ├── 获取 per-server Lock
        ├── double-check：其他协程可能已刷新
        ├── _fetch_token(oauth)
        │   ├── 构建 POST 请求参数
        │   ├── httpx.post(token_url, data)
        │   ├── 解析响应（字段名可配置）
        │   └── 计算 expires_at = now + expires_in
        ├── 缓存新令牌
        └── return "{token_type} {access_token}"
```

### Double-check locking

为什么需要两次检查缓存：
```
协程 A                          协程 B
  │                               │
  ├─ 检查缓存: 过期               │
  ├─ 等待 Lock...                │
  │                               ├─ 检查缓存: 过期
  │                               ├─ 获取 Lock
  │                               ├─ _fetch_token()
  │                               ├─ 更新缓存
  │                               └─ 释放 Lock
  ├─ 获取 Lock                    │
  ├─ 再次检查缓存: 有效!          │
  └─ 直接返回（不重复获取）        │
```

### 提前刷新策略

`_is_expiring()` 在令牌实际过期前 `refresh_skew_seconds` 秒就返回 True（默认 60 秒）。这避免了以下边界情况：
- 令牌获取后因网络延迟，到达服务器时已过期
- 本地时钟与令牌服务器时钟有偏差
- 长时间操作中令牌过期

### 支持的授权类型

| 授权类型 | 必须字段 | 请求参数 | 适用场景 |
|----------|----------|----------|----------|
| `client_credentials` | `client_id`, `client_secret` | `grant_type` + 凭证 | 服务器间通信（无用户上下文） |
| `refresh_token` | `refresh_token` | `grant_type` + refresh_token + 可选凭证 | 长期访问（有用户上下文） |

### 可配置的响应字段

不同 OAuth 提供商的令牌响应格式可能不同。以下字段名均可配置：

| 配置字段 | 默认值 | 说明 |
|----------|--------|------|
| `token_field` | `"access_token"` | 令牌值的字段名 |
| `token_type_field` | `"token_type"` | 令牌类型的字段名 |
| `expires_in_field` | `"expires_in"` | 过期时间（秒）的字段名 |
| `default_token_type` | `"Bearer"` | 令牌类型缺失时的默认值 |

### 额外参数

`extra_token_params` 允许向令牌请求添加自定义参数，适配非标准 OAuth 提供商。

## build_oauth_tool_interceptor — 拦截器构建

### 拦截器签名

```python
async def interceptor(request: Any, handler: Any) -> Any
```

符合 `langchain-mcp-adapters` 的工具拦截器协议。

### 工作流程

```
MCP 工具调用
    │
    ▼
oauth_interceptor(request, handler)
    │
    ├── get_authorization_header(request.server_name)
    │   └── 获取（或刷新）令牌
    │
    ├── 无令牌（服务器不需要 OAuth）
    │   └── 直接透传: handler(request)
    │
    └── 有令牌
        ├── 注入 Authorization 头
        └── handler(request.override(headers=updated_headers))
```

### 为什么用拦截器

langchain-mcp-adapters 的工具调用发生在框架内部，上层代码无法在每次调用前手动添加认证头。拦截器是框架提供的扩展点，可以在工具调用前后执行自定义逻辑。

### 为什么还需要初始认证头

拦截器只在后续的工具调用中生效。但 MCP 协议在建立连接后立即进行工具发现（tool discovery），如果服务器要求认证，连接建立时就需要携带有效令牌。`get_initial_oauth_headers()` 解决的就是这个问题。

## get_initial_oauth_headers — 初始认证头

在 MCP 客户端初始化前调用，为所有 OAuth 服务器预获取令牌。返回 `{server_name: "Bearer xxx"}` 映射，由 `tools.py` 注入到 `servers_config` 的 headers 中。

只处理 SSE/HTTP 传输的服务器（stdio 传输不需要 HTTP 认证头）。

## 配置示例

```json
{
  "type": "sse",
  "url": "https://api.example.com/mcp",
  "oauth": {
    "enabled": true,
    "token_url": "https://auth.example.com/oauth/token",
    "grant_type": "client_credentials",
    "client_id": "my-client-id",
    "client_secret": "$OAUTH_SECRET",
    "scope": "read write",
    "refresh_skew_seconds": 120,
    "extra_token_params": {
      "resource": "https://api.example.com"
    }
  }
}
```
