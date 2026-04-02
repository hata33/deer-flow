# MCP 模块文件清单

## 模块概述

MCP (Model Context Protocol) 模块集成 langchain-mcp-adapters，支持多服务器管理、OAuth 认证和工具延迟加载。

## 文件清单

### 1. `/data/deer-flow-main/backend/packages/harness/deerflow/mcp/__init__.py`

**核心导出**:
- `build_server_params` - 构建服务器参数
- `build_servers_config` - 构建服务器配置
- `get_mcp_tools` - 获取 MCP 工具
- `initialize_mcp_tools` - 初始化 MCP 工具
- `get_cached_mcp_tools` - 获取缓存的 MCP 工具
- `reset_mcp_tools_cache()` - 重置缓存

**职责**: MCP 模块的统一导出入口

---

### 2. `/data/deer-flow-main/backend/packages/harness/deerflow/mcp/cache.py`

**核心类/函数**:
- `_mcp_tools_cache` - 工具缓存
- `_cache_initialized` - 是否已初始化
- `_config_mtime` - 配置文件修改时间
- `_get_config_mtime()` - 获取配置文件修改时间
- `_is_cache_stale()` - 检查缓存是否过期
- `initialize_mcp_tools()` - 异步初始化 MCP 工具
- `get_cached_mcp_tools()` - 获取缓存工具（带延迟初始化）
- `reset_mcp_tools_cache()` - 重置缓存

**职责**: MCP 工具缓存管理，支持配置文件热更新

---

### 3. `/data/deer-flow-main/backend/packages/harness/deerflow/mcp/client.py`

**核心类/函数**:
- `build_server_params(server_name, config)` - 构建单个服务器参数
- `build_servers_config(extensions_config)` - 构建所有服务器配置

**职责**: MCP 客户端配置构建

---

### 4. `/data/deer-flow-main/backend/packages/harness/deerflow/mcp/oauth.py`

**核心类/函数**:
- `OAuthTokenManager` - OAuth Token 管理器
  - `from_extensions_config()` - 从配置创建
  - `has_oauth_servers()` - 检查是否有 OAuth 服务器
  - `oauth_server_names()` - 获取 OAuth 服务器名称列表
  - `get_authorization_header()` - 获取 Authorization 头
  - `_fetch_token()` - 获取 Token
- `build_oauth_tool_interceptor()` - 构建 OAuth 工具拦截器
- `get_initial_oauth_headers()` - 获取初始 OAuth 头

**职责**: OAuth Token 管理，支持 client_credentials 和 refresh_token 授权

---

### 5. `/data/deer-flow-main/backend/packages/harness/deerflow/mcp/tools.py`

**核心类/函数**:
- `_SYNC_TOOL_EXECUTOR` - 全局线程池（同步工具调用）
- `_make_sync_tool_wrapper()` - 创建同步包装器
- `get_mcp_tools()` - 异步获取所有 MCP 工具
  - 使用 MultiServerMCPClient
  - 注入 OAuth 头
  - 包装异步工具为同步

**职责**: MCP 工具加载和同步包装

---

## 配置支持

**传输类型**:
- `stdio` - 命令行标准输入/输出
- `sse` - Server-Sent Events
- `http` - HTTP

**OAuth 支持**:
- `client_credentials` - 客户端凭证
- `refresh_token` - 刷新令牌
- 自动 Token 刷新
