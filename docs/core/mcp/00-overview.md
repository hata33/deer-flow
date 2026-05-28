# MCP 集成模块 — 全局概览

## 定位

DeerFlow MCP（Model Context Protocol）集成模块（`deerflow.mcp`）是 DeerFlow Agent 与外部工具服务之间的桥梁。它负责从 `extensions_config.json` 声明的 MCP 服务器中发现、加载和管理外部工具，使 Agent 能够无缝调用第三方能力（如文件系统、数据库、搜索引擎、企业内部 API 等）。

> **关键边界**：MCP 模块只管理"工具的发现和调用"，不管理"工具的选择和编排"。后者由 Agent 的工具系统和中间件链负责。

## 解决的核心问题

| 问题 | MCP 模块的解决方案 |
|------|---------------------|
| **外部工具接入成本高** | MCP 是标准化协议，任何符合协议的服务都可直接接入。DeerFlow 通过 langchain-mcp-adapters 自动发现工具的名称、参数和描述，无需手写集成代码 |
| **多服务器多工具管理** | `MultiServerMCPClient` 支持同时连接多个 MCP 服务器，统一管理工具发现和调用。工具名自动添加服务器前缀避免冲突 |
| **认证复杂性** | OAuth 模块自动处理令牌获取、缓存、刷新，通过拦截器在每次调用时透明注入认证头。支持 client_credentials 和 refresh_token 两种授权类型 |
| **配置热更新** | Gateway API（独立进程）修改配置后，LangGraph Server 通过 mtime 比对检测变化，自动失效缓存并重新加载工具 |
| **同步/异步兼容** | MCP 工具是异步的，但 DeerFlow 嵌入式客户端（DeerFlowClient）在同步上下文中使用。`make_sync_tool_wrapper` 自动处理桥接 |
| **多事件循环环境** | 工具加载可能发生在 FastAPI 事件循环或 LangGraph Studio 的独立循环中。懒加载逻辑处理所有可能的运行环境 |

## 能力来源

MCP 模块的能力建立在以下技术栈之上：

```
langchain-mcp-adapters
  ├── MultiServerMCPClient   — 多服务器 MCP 客户端
  ├── tool_interceptors      — 工具调用拦截器（认证注入）
  └── tool_name_prefix       — 工具名前缀（避免冲突）

langchain-core
  └── BaseTool               — 工具基类（统一的调用接口）

httpx                        — OAuth 令牌请求（异步 HTTP 客户端）

DeerFlow 配置系统
  ├── ExtensionsConfig       — MCP 服务器 + Skills 声明式配置
  ├── McpServerConfig        — 单服务器配置（传输类型、命令/URL、OAuth）
  └── McpOAuthConfig         — OAuth 配置（令牌端点、凭证、刷新策略）

DeerFlow 反射系统
  └── resolve_variable()     — 动态加载自定义拦截器
```

## 架构总览

```
mcp/
├── __init__.py     # 公开 API 入口
├── cache.py        # 工具缓存管理（懒加载、mtime 热更新、多事件循环兼容）
├── client.py       # 客户端配置构建（传输参数映射、配置校验）
├── oauth.py        # OAuth 令牌管理（获取、缓存、刷新、拦截器注入）
└── tools.py        # 工具加载入口（整合配置、客户端、OAuth、拦截器、同步包装）
```

### 模块间调用关系

```
Agent 工具系统 (get_available_tools)
    │
    ▼
get_cached_mcp_tools()                    ← cache.py
    │
    ├── 检查缓存是否过期（mtime 比对）
    │   └── 过期 → reset_mcp_tools_cache()
    │
    ├── 缓存未初始化 → 懒加载
    │   └── initialize_mcp_tools()
    │       └── get_mcp_tools()            ← tools.py（核心入口）
    │           │
    │           ├── ExtensionsConfig.from_file()   ← 从磁盘读取最新配置
    │           │
    │           ├── build_servers_config()          ← client.py
    │           │   └── build_server_params()       ← 传输参数映射
    │           │
    │           ├── get_initial_oauth_headers()     ← oauth.py
    │           │   └── OAuthTokenManager           ← 令牌获取/缓存
    │           │
    │           ├── build_oauth_tool_interceptor()  ← oauth.py
    │           │   └── oauth_interceptor           ← 每次调用注入认证头
    │           │
    │           ├── resolve_variable()              ← 自定义拦截器加载
    │           │
    │           ├── MultiServerMCPClient            ← langchain-mcp-adapters
    │           │   └── get_tools()                 ← 工具发现
    │           │
    │           └── make_sync_tool_wrapper()        ← 异步→同步包装
    │
    └── 返回缓存的工具列表
```

## 四大子模块

### 1. cache.py — 工具缓存管理

- **职责**: 管理 MCP 工具的全局缓存，避免重复加载
- **核心机制**: mtime 比对检测配置热更新 + 多事件循环兼容的懒加载
- **详见**: [01-cache.md](01-cache.md)

### 2. client.py — 客户端配置构建

- **职责**: 将 Pydantic 配置模型转换为 langchain-mcp-adapters 参数字典
- **传输支持**: stdio（子进程）、sse（Server-Sent Events）、http（HTTP 流式）
- **详见**: [02-client.md](02-client.md)

### 3. oauth.py — OAuth 令牌管理

- **职责**: 为需要认证的 MCP 服务器自动获取和管理 OAuth 令牌
- **核心机制**: double-check locking + 提前刷新 + 拦截器注入
- **详见**: [03-oauth.md](03-oauth.md)

### 4. tools.py — 工具加载入口

- **职责**: 整合所有子模块，执行完整的工具加载流程
- **核心流程**: 配置读取 → 客户端构建 → OAuth 注入 → 拦截器链 → 工具发现 → 同步包装
- **详见**: [04-tools.md](04-tools.md)

## 传输方式

| 传输类型 | 适用场景 | 连接方式 | 认证方式 |
|----------|----------|----------|----------|
| `stdio` | 本地工具（文件系统、Git 等） | 启动子进程，stdin/stdout 通信 | 环境变量（`env`） |
| `sse` | 远程工具（企业 API、云端服务） | Server-Sent Events 长连接 | HTTP headers + OAuth |
| `http` | 远程工具（HTTP 流式传输） | HTTP 请求/响应 | HTTP headers + OAuth |

## 配置体系

MCP 服务器在 `extensions_config.json` 中声明：

```json
{
  "mcpServers": {
    "filesystem": {
      "enabled": true,
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"],
      "env": {"API_KEY": "$FILESYSTEM_API_KEY"}
    },
    "remote-api": {
      "enabled": true,
      "type": "sse",
      "url": "https://api.example.com/mcp",
      "headers": {"X-Custom": "value"},
      "oauth": {
        "enabled": true,
        "token_url": "https://auth.example.com/oauth/token",
        "grant_type": "client_credentials",
        "client_id": "my-client-id",
        "client_secret": "$OAUTH_CLIENT_SECRET"
      }
    }
  }
}
```

配置优先级：`DEER_FLOW_EXTENSIONS_CONFIG_PATH` 环境变量 > `extensions_config.json` > `mcp_config.json`（向后兼容）

## 错误处理策略

| 场景 | 行为 |
|------|------|
| `langchain-mcp-adapters` 未安装 | 返回空列表 + 警告日志 |
| 没有已启用的服务器 | 返回空列表 |
| 单个服务器配置无效 | 跳过该服务器 + 错误日志，不影响其他 |
| OAuth 令牌获取失败 | 抛出异常（由调用方处理） |
| 整体工具加载失败 | 返回空列表 + 错误日志，Agent 其他功能不受影响 |

## 与其他系统的关系

```
                    ┌──────────────────────────┐ 
                    │     Agent 工具系统        │
                    │  get_available_tools()   │
                    └────────┬─────────────────┘
                             │ 查询 MCP 工具
                    ┌────────▼─────────────────┐
                    │       MCP 模块           │ ◄── 本文档范围
                    │  cache / client / oauth  │
                    │       / tools            │
                    └────────┬─────────────────┘
                             │ 连接
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌──────────┐  ┌──────────┐  ┌──────────────┐
        │ MCP 服务器│ │ OAuth 端点│  │ Extensions   │
        │ (stdio)  │  │ (httpx)  │  │ Config (磁盘) │
        └──────────┘  └──────────┘  └──────────────┘
              ▲              ▲              ▲
        ┌─────┴──────────────┴──────────────┴─────┐
        │      langchain-mcp-adapters 框架         │
        └─────────────────────────────────────────┘
```

## 相关文档

- [05-lifecycle.md](05-lifecycle.md) — 完整的 MCP 工具加载与调用生命周期
