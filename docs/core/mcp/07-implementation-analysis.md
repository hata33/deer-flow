# 07 - MCP 实现机制深度分析

> 本文档基于 `backend/packages/harness/deerflow/mcp/` 目录下的源码，逐层拆解 MCP 集成的实现细节。回答的是"代码怎么写的、为什么这么写"。

---

## 一、模块依赖关系图

```
┌─────────────────────────────────────────────────────────────────┐
│                      调用方（外部世界）                           │
│                                                                  │
│  tools/builtins/                agents/lead_agent/agent.py      │
│  ┌────────────────────────┐    ┌───────────────────────────┐   │
│  │ get_available_tools()  │    │ make_lead_agent()         │   │
│  │  └─ include_mcp=True   │    │  └─ get_available_tools() │   │
│  └──────────┬─────────────┘    └──────────┬────────────────┘   │
│             │                              │                    │
│             │ ①获取 MCP 工具               │                    │
└─────────────┼──────────────────────────────┼────────────────────┘
              │                              │
┌─────────────▼──────────────────────────────▼────────────────────┐
│                      mcp 包（内部世界）                           │
│                                                                   │
│  __init__.py ─── 统一导出入口                                      │
│                                                                   │
│  ┌──────────────┐                                                │
│  │ cache.py     │ ── 主入口：get_cached_mcp_tools()             │
│  │              │    mtime 检测 → 懒加载 → 缓存                  │
│  │              │                                                │
│  │  ③ 缓存管理  │                                                │
│  │  ④ 事件循环  │                                                │
│  └──────┬───────┘                                                │
│         │ ②首次加载时调用                                         │
│         │                                                        │
│  ┌──────▼───────┐   ┌──────────────┐   ┌───────────────────┐   │
│  │ tools.py     │   │ client.py    │   │ oauth.py          │   │
│  │              │   │              │   │                   │   │
│  │ ⑤ 整合入口   │   │ ⑥ 参数构建   │   │ ⑦ Token 管理     │   │
│  │  同步包装    │   │  传输映射    │   │  拦截器注入       │   │
│  └──────────────┘   └──────────────┘   └───────────────────┘   │
│                                                                   │
│  ┌──────────────────────────────────────────────────────┐       │
│  │ 外部依赖: langchain-mcp-adapters.MultiServerMCPClient│       │
│  └──────────────────────────────────────────────────────┘       │
└───────────────────────────────────────────────────────────────────┘
```

---

## 二、第 1 层：缓存管理 — cache.py

### 2.1 缓存流程

`get_cached_mcp_tools()` 是所有 MCP 工具访问的唯一入口，执行以下步骤：

```
get_cached_mcp_tools()
      │
      ├─ ① _is_cache_stale()
      │    └─ 比较 _config_mtime 与当前文件 mtime
      │    └─ 如果 current_mtime > _config_mtime → 缓存过期
      │         └─ reset_mcp_tools_cache()
      │              └─ _mcp_tools_cache = None
      │              └─ _cache_initialized = False
      │              └─ _config_mtime = None
      │
      ├─ ② 检查 _cache_initialized
      │    └─ True → 直接返回 _mcp_tools_cache
      │    └─ False → 执行懒加载
      │
      └─ ③ 懒加载（三种事件循环路径）
           ├─ loop.is_running() → ThreadPoolExecutor + asyncio.run
           ├─ loop 存在未运行 → loop.run_until_complete
           └─ 无 loop → asyncio.run
```

**全局状态变量**：

| 变量 | 类型 | 作用 |
|------|------|------|
| `_mcp_tools_cache` | `list[BaseTool] \| None` | 缓存的工具列表 |
| `_cache_initialized` | `bool` | 是否已完成首次初始化 |
| `_config_mtime` | `float \| None` | 配置文件 mtime 快照 |
| `_initialization_lock` | `asyncio.Lock` | 防止并发初始化 |

### 2.2 mtime 检测逻辑

```python
def _is_cache_stale() -> bool:
    # 未初始化不算过期
    if not _cache_initialized:
        return False

    current_mtime = _get_config_mtime()
    # 无法获取 mtime → 保守地不过期
    if _config_mtime is None or current_mtime is None:
        return False
    # 文件比缓存新 → 过期
    return current_mtime > _config_mtime
```

**保守策略**：mtime 获取失败时不认为过期（文件可能被删除或路径错误），避免循环重试。

---

## 三、第 2 层：工具加载入口 — tools.py

### 3.1 get_mcp_tools() 完整流程

```
get_mcp_tools()
      │
      ├─ ① 检查依赖
      │    └─ from langchain_mcp_adapters.client import MultiServerMCPClient
      │    └─ ImportError → 返回 []
      │
      ├─ ② 从磁盘读取最新配置
      │    └─ ExtensionsConfig.from_file()  ← 直接读磁盘，不用内存缓存
      │    └─ build_servers_config()
      │
      ├─ ③ 注入 OAuth 初始认证头
      │    └─ get_initial_oauth_headers()
      │    └─ 对 SSE/HTTP 类型注入 Authorization header
      │
      ├─ ④ 构建拦截器链
      │    ├─ 4a. OAuth 拦截器
      │    │    └─ build_oauth_tool_interceptor()
      │    └─ 4b. 自定义拦截器
      │         └─ extensions_config.model_extra["mcpInterceptors"]
      │         └─ resolve_variable() 反射加载
      │
      ├─ ⑤ 创建 MultiServerMCPClient
      │    └─ tool_name_prefix=True → "server__tool" 前缀防冲突
      │    └─ await client.get_tools()
      │
      └─ ⑥ 同步包装
           └─ 检测 tool.func is None and tool.coroutine is not None
           └─ make_sync_tool_wrapper(tool.coroutine, tool.name)
```

**为什么每次从磁盘读取配置**：`get_mcp_tools()` 只在缓存未命中时被调用（频率很低），此时需要确保读到 Gateway API（独立进程）写入的最新配置。`ExtensionsConfig.from_file()` 直接解析磁盘文件，跳过进程内缓存。

### 3.2 同步包装机制

MCP 工具只有 `coroutine`（异步实现），没有 `func`（同步实现）。但 DeerFlowClient 在同步上下文中使用工具。`make_sync_tool_wrapper()` 将异步 coroutine 包装为同步 func，内部使用线程池处理异步调用。

```python
for tool in tools:
    if getattr(tool, "func", None) is None and getattr(tool, "coroutine", None) is not None:
        tool.func = make_sync_tool_wrapper(tool.coroutine, tool.name)
```

---

## 四、第 3 层：客户端配置构建 — client.py

### 4.1 传输参数映射

`build_server_params()` 根据传输类型构建不同结构的参数字典：

```
输入：McpServerConfig（Pydantic 模型）
      │
      ├─ type == "stdio"
      │    → {"transport": "stdio", "command": "npx", "args": [...], "env": {...}}
      │    必须字段：command
      │    可选字段：args, env
      │
      ├─ type == "sse"
      │    → {"transport": "sse", "url": "https://...", "headers": {...}}
      │    必须字段：url
      │    可选字段：headers
      │
      ├─ type == "http"
      │    → {"transport": "http", "url": "https://...", "headers": {...}}
      │    必须字段：url
      │    可选字段：headers
      │
      └─ 其他 → ValueError
```

### 4.2 批量构建与容错

`build_servers_config()` 遍历所有 `enabled=true` 的服务器，逐个构建参数。单个服务器失败只记录日志，不阻止其他服务器：

```python
for server_name, server_config in enabled_servers.items():
    try:
        servers_config[server_name] = build_server_params(server_name, server_config)
    except Exception as e:
        logger.error(f"Failed to configure MCP server '{server_name}': {e}")
```

---

## 五、第 4 层：OAuth 令牌管理 — oauth.py

### 5.1 令牌生命周期

```
OAuthTokenManager
      │
      ├─ get_authorization_header(server_name)
      │    │
      │    ├─ 快速路径：缓存命中且未过期
      │    │    └─ return f"{token.token_type} {token.access_token}"
      │    │
      │    └─ 慢速路径：需要刷新
      │         ├─ async with lock  ← per-server 锁
      │         ├─ double-check：再次确认缓存
      │         └─ _fetch_token(oauth) → _OAuthToken
      │              └─ httpx.post(token_url, data={...})
      │              └─ 解析响应：token_field, token_type_field, expires_in_field
      │              └─ expires_at = now + timedelta(seconds=expires_in)
      │
      └─ _is_expiring(token, oauth)
           └─ token.expires_at <= now + timedelta(seconds=refresh_skew_seconds)
           └─ 默认提前 60 秒刷新
```

### 5.2 支持的授权类型

| 授权类型 | 必需参数 | 用途 |
|----------|----------|------|
| `client_credentials` | client_id, client_secret | 服务器间通信（最常见） |
| `refresh_token` | refresh_token, (client_id) | 长期访问 |

两种类型都支持 `extra_token_params` 传递自定义参数，以及 `scope` 和 `audience` 字段。

### 5.3 拦截器注入流程

```
build_oauth_tool_interceptor() → oauth_interceptor
      │
      │   每次 MCP 工具调用时触发
      ▼
oauth_interceptor(request, handler)
      │
      ├─ header = await token_manager.get_authorization_header(request.server_name)
      │
      ├─ header is None → 该服务器不需要 OAuth → 直接透传
      │
      └─ header is not None
           └─ updated_headers["Authorization"] = header
           └─ request.override(headers=updated_headers)
           └─ await handler(modified_request)
```

### 5.4 初始认证头获取

```
get_initial_oauth_headers()
      │
      ├─ 遍历所有 OAuth 服务器
      │    └─ await token_manager.get_authorization_header(name)
      │
      └─ 过滤掉空值（获取失败的）
           └─ return {name: "Bearer xxx", ...}
```

在 `get_mcp_tools()` 中，这些 headers 被注入到 SSE/HTTP 服务器的配置中，确保初始连接（工具发现阶段）就能通过认证。

---

## 六、配置热更新完整追踪

以运维人员通过 Gateway API 添加新 MCP 服务器为例：

```
T+0.0s  PUT /api/mcp/config {"mcpServers": {"new-svc": {...}}}
          → Gateway 写入 extensions_config.json（原子操作）
          → 文件 mtime 更新

T+5.0s  Agent 对话请求到达
          → make_lead_agent()
            → get_available_tools(include_mcp=True)
              → get_cached_mcp_tools()
                → _is_cache_stale()
                  → current_mtime > _config_mtime  ← 检测到变更
                  → reset_mcp_tools_cache()         ← 清空缓存
                → _cache_initialized == False
                  → 懒加载触发
                  → get_mcp_tools()
                    → ExtensionsConfig.from_file()   ← 读到新配置
                    → build_servers_config()          ← 包含 new-svc
                    → MultiServerMCPClient(...)       ← 建立新连接
                    → tools = await client.get_tools() ← 发现新工具
                → 缓存更新，新工具可用
```

---

## 七、文件职责速查表

| 文件 | 代码行 | 核心职责 | 关键类/函数 |
|------|--------|----------|------------|
| `cache.py` | ~200 | 缓存管理 + 懒加载 | `get_cached_mcp_tools()`, `_is_cache_stale()`, `reset_mcp_tools_cache()` |
| `tools.py` | ~160 | 工具加载整合入口 | `get_mcp_tools()` |
| `client.py` | ~123 | 传输参数映射 | `build_server_params()`, `build_servers_config()` |
| `oauth.py` | ~330 | OAuth 令牌管理 | `OAuthTokenManager`, `build_oauth_tool_interceptor()`, `get_initial_oauth_headers()` |
