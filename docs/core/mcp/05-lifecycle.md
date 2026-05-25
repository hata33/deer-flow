# MCP 生命周期

本文描述 MCP 工具从配置声明到 Agent 调用的完整生命周期。

## 一、配置阶段（部署/管理员操作）

```
管理员编写 extensions_config.json
    │
    ├── 声明 MCP 服务器
    │   ├── name: 服务器唯一标识（如 "filesystem"）
    │   ├── type: 传输类型（"stdio" | "sse" | "http"）
    │   ├── enabled: 是否启用
    │   │
    │   ├── stdio 类型:
    │   │   ├── command: 启动命令（如 "npx"）
    │   │   ├── args: 命令参数（如 ["-y", "@mcp/server-filesystem"]）
    │   │   └── env: 环境变量（如 {"API_KEY": "$ENV_VAR"}）
    │   │
    │   └── sse/http 类型:
    │       ├── url: 服务器 URL
    │       ├── headers: 自定义 HTTP 头
    │       └── oauth: OAuth 配置（可选）
    │           ├── token_url: 令牌端点
    │           ├── grant_type: 授权类型
    │           ├── client_id / client_secret: 凭证
    │           └── refresh_skew_seconds: 提前刷新时间
    │
    └── 配置文件保存到磁盘
        └── 优先级: 环境变量 > 项目根目录 > backend 目录
```

## 二、工具加载阶段（Agent 初始化）

```
Agent 初始化（get_available_tools）
    │
    ▼
get_cached_mcp_tools()                         ← cache.py
    │
    ├── 检查缓存是否过期
    │   ├── _is_cache_stale()
    │   │   ├── 获取当前 extensions_config.json 的 mtime
    │   │   ├── 与缓存中记录的 mtime 比对
    │   │   └── 当前 mtime > 缓存 mtime → 过期
    │   │
    │   └── 过期 → reset_mcp_tools_cache()
    │       ├── _mcp_tools_cache = None
    │       ├── _cache_initialized = False
    │       └── _config_mtime = None
    │
    ├── 缓存未初始化 → 懒加载
    │   │
    │   ├── 事件循环已运行？
    │   │   ├── 是 → ThreadPoolExecutor + asyncio.run（独立线程）
    │   │   ├── 否但有循环 → loop.run_until_complete
    │   │   └── 无循环 → asyncio.run
    │   │
    │   └── initialize_mcp_tools()
    │       ├── 获取 asyncio.Lock（防并发）
    │       └── get_mcp_tools()                 ← tools.py（核心）
    │
    └── 返回缓存的工具列表
```

### get_mcp_tools() 核心加载流程

```
get_mcp_tools()
    │
    ├── 1. 检查 langchain-mcp-adapters 是否安装
    │   └── 未安装 → 返回 []
    │
    ├── 2. ExtensionsConfig.from_file()         ← 从磁盘读取（不用缓存）
    │   ├── resolve_config_path() 定位文件
    │   ├── json.load() 解析 JSON
    │   ├── resolve_env_variables() 解析 $ENV_VAR
    │   └── Pydantic 校验
    │
    ├── 3. build_servers_config()               ← client.py
    │   ├── 过滤 enabled=true 的服务器
    │   └── 逐一调用 build_server_params()
    │       ├── stdio → {transport, command, args, env}
    │       ├── sse   → {transport, url, headers}
    │       └── http  → {transport, url, headers}
    │
    ├── 4. get_initial_oauth_headers()          ← oauth.py
    │   ├── OAuthTokenManager.from_extensions_config()
    │   ├── 遍历所有 OAuth 服务器
    │   ├── 获取/刷新令牌
    │   └── 注入到 servers_config 的 headers 中
    │
    ├── 5. 构建拦截器链
    │   ├── build_oauth_tool_interceptor()
    │   │   └── oauth_interceptor: 每次调用时注入 Authorization 头
    │   │
    │   └── 加载自定义拦截器（mcpInterceptors 字段）
    │       └── resolve_variable("pkg.module:func") → builder() → interceptor
    │
    ├── 6. MultiServerMCPClient(config, interceptors)
    │   ├── 启动 stdio 子进程 / 建立 SSE 连接
    │   ├── 应用拦截器
    │   └── get_tools() → 发现所有工具
    │       └── 工具名添加前缀: "{server_name}__{tool_name}"
    │
    ├── 7. make_sync_tool_wrapper()
    │   └── 为只有 coroutine 的工具添加同步 func 包装
    │       └── 内部使用 ThreadPoolExecutor + asyncio.run
    │
    └── 返回工具列表
```

## 三、OAuth 令牌生命周期

```
首次获取令牌
    │
    ▼
OAuthTokenManager.get_authorization_header(server_name)
    │
    ├── 检查缓存
    │   └── 有缓存且未过期 → 直接返回（快速路径）
    │
    ├── 加锁（per-server asyncio.Lock）
    │
    ├── 再次检查缓存（double-check locking）
    │   └── 其他协程可能已在等待锁期间刷新了令牌
    │
    ├── _fetch_token(oauth)
    │   ├── 构建 HTTP POST 请求
    │   │   ├── grant_type=client_credentials
    │   │   │   └── 发送 client_id + client_secret
    │   │   └── grant_type=refresh_token
    │   │       └── 发送 refresh_token + 可选 client_id/secret
    │   │
    │   ├── httpx.post(token_url, data)
    │   │
    │   └── 解析响应
    │       ├── access_token ← token_field（默认 "access_token"）
    │       ├── token_type ← token_type_field（默认 "token_type"）
    │       └── expires_at ← now + expires_in 秒
    │
    ├── 缓存令牌
    └── 返回 "{token_type} {access_token}"


令牌自动刷新（在过期前 refresh_skew_seconds 秒触发）
    │
    ▼
_is_expiring(token, oauth)
    │
    ├── token.expires_at <= now + refresh_skew_seconds
    │   └── True → 需要刷新
    │
    └── 触发 _fetch_token() 获取新令牌


拦截器注入（每次 MCP 工具调用）
    │
    ▼
oauth_interceptor(request, handler)
    │
    ├── get_authorization_header(request.server_name)
    │   └── 获取（或刷新）令牌
    │
    ├── 更新 request.headers["Authorization"]
    │
    └── handler(request.override(headers=updated_headers))
```

## 四、工具调用阶段（Agent 运行时）

```
Agent 选择使用 MCP 工具
    │
    ▼
LangChain 工具调用
    │
    ├── 异步路径（FastAPI / LangGraph）
    │   └── tool.coroutine(**kwargs)
    │       └── langchain-mcp-adapters 处理:
    │           ├── 拦截器预处理（OAuth 注入认证头）
    │           ├── 传输请求
    │           │   ├── stdio: 写入子进程 stdin
    │           │   └── sse/http: 发送 HTTP 请求
    │           ├── 等待响应
    │           └── 返回结果
    │
    └── 同步路径（DeerFlowClient 嵌入式客户端）
        └── tool.func(**kwargs)               ← make_sync_tool_wrapper 包装
            ├── 检查当前事件循环
            │   ├── 循环已运行 → ThreadPoolExecutor + asyncio.run
            │   └── 无循环 → 直接 asyncio.run
            └── 内部调用 tool.coroutine(**kwargs)
```

## 五、配置热更新生命周期

```
Gateway API 修改 extensions_config.json
    │
    ├── PUT /api/mcp/config → 更新 MCP 服务器配置
    │   └── 保存到磁盘（extensions_config.json）
    │
    ▼
LangGraph Server（独立进程）下次获取工具时
    │
    ▼
get_cached_mcp_tools()
    │
    ├── _is_cache_stale()
    │   ├── 当前 mtime > 缓存 mtime → True
    │   └── 检测到配置已变更
    │
    ├── reset_mcp_tools_cache()
    │   └── 清空缓存
    │
    └── 懒加载触发重新初始化
        └── 从磁盘重新读取配置 → 重新构建客户端 → 重新发现工具
```

## 六、错误处理生命周期

```
get_mcp_tools() 中的错误处理
    │
    ├── langchain-mcp-adapters 未安装
    │   └── 返回 [] + 警告日志
    │       └── Agent 正常运行，只是没有 MCP 工具
    │
    ├── build_server_params() 失败（配置错误）
    │   └── 跳过该服务器 + 错误日志
    │       └── 其他服务器继续加载
    │
    ├── OAuth 令牌获取失败
    │   ├── httpx 网络错误
    │   ├── 令牌端点返回 4xx/5xx
    │   └── 响应缺少必要字段
    │   └── 异常向上传播（由 get_mcp_tools 的 try/except 捕获）
    │
    ├── 自定义拦截器加载失败
    │   └── 跳过该拦截器 + 警告日志
    │       └── 其他拦截器继续加载
    │
    ├── MultiServerMCPClient 初始化失败
    │   ├── 服务器不可达
    │   ├── 子进程启动失败
    │   └── 协议握手失败
    │   └── 返回 [] + 错误日志
    │
    └── get_cached_mcp_tools() 懒加载失败
        └── 返回 [] + 错误日志
            └── Agent 其他功能（内置工具、沙箱等）不受影响
```

## 七、缓存状态机

```
                    reset_mcp_tools_cache()
                           │
                           ▼
    ┌─────────────────────────────────────────┐
    │         未初始化（_cache_initialized=False）│
    └────────────────┬────────────────────────┘
                     │
                     │ get_cached_mcp_tools()
                     │ 触发懒加载
                     │
                     ▼
    ┌─────────────────────────────────────────┐
    │       初始化中（_initialization_lock）     │
    │  get_mcp_tools() 执行中                   │
    └────────────────┬────────────────────────┘
                     │
                     │ 初始化完成
                     │ 记录 _config_mtime
                     │
                     ▼
    ┌─────────────────────────────────────────┐
    │         已缓存（_cache_initialized=True）  │
    │  直接返回 _mcp_tools_cache               │
    └────────────────┬────────────────────────┘
                     │
                     │ _is_cache_stale() = True
                     │ （配置文件 mtime 变更）
                     │
                     ▼
              reset_mcp_tools_cache()
                     │
                     └──→ 回到"未初始化"状态
```
