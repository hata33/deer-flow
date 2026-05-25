# Tools — 工具加载入口

## 模块路径

`deerflow.mcp.tools`

## 解决的问题

本模块是 MCP 工具加载的核心入口，整合所有子模块完成从配置到可用工具的完整转换。它协调了配置读取、客户端构建、OAuth 注入、拦截器链、工具发现和同步包装六个步骤。

## 核心函数

### `get_mcp_tools()` — 工具加载主流程

这是唯一对外暴露的函数，被 `cache.py` 的 `initialize_mcp_tools()` 调用。

执行流程（六步）：

#### 步骤 1: 依赖检查

```python
from langchain_mcp_adapters.client import MultiServerMCPClient
```

未安装 → 返回 `[]` + 警告日志。

#### 步骤 2: 从磁盘读取最新配置

```python
extensions_config = ExtensionsConfig.from_file()
```

关键：使用 `from_file()` 而非 `get_extensions_config()`。前者每次从磁盘读取，后者使用内存缓存。选择前者的原因是：
- Gateway API（修改配置的进程）和 LangGraph Server（使用配置的进程）运行在独立进程中
- 内存缓存在不同进程间不共享
- 从磁盘读取确保获取最新的配置变更

#### 步骤 3: 客户端配置构建

```python
servers_config = build_servers_config(extensions_config)
```

将 Pydantic 配置转换为 `MultiServerMCPClient` 参数字典。详见 [02-client.md](02-client.md)。

#### 步骤 4: OAuth 初始认证头注入

```python
initial_oauth_headers = await get_initial_oauth_headers(extensions_config)
```

在 SSE/HTTP 连接建立前获取 OAuth 令牌，注入到 `servers_config` 的 headers 中。这确保工具发现（tool discovery）阶段就能通过认证。

只对 `transport in ("sse", "http")` 的服务器注入（stdio 不需要 HTTP 认证头）。

#### 步骤 5: 拦截器链构建

```python
tool_interceptors = []
```

**5a. OAuth 拦截器**：
```python
oauth_interceptor = build_oauth_tool_interceptor(extensions_config)
if oauth_interceptor is not None:
    tool_interceptors.append(oauth_interceptor)
```

在每次工具调用时自动注入 Authorization 头。详见 [03-oauth.md](03-oauth.md)。

**5b. 自定义拦截器**：
```python
raw_interceptor_paths = extensions_config.model_extra.get("mcpInterceptors")
```

从 `extensions_config.json` 的 `mcpInterceptors` 字段加载。格式：
```json
{
  "mcpInterceptors": [
    "my_package.interceptors:build_logging_interceptor",
    "my_package.auth:build_custom_auth_interceptor"
  ]
}
```

加载流程：
1. `resolve_variable("my_package.interceptors:build_logging_interceptor")` 解析构建函数
2. `builder()` 调用构建函数，获取拦截器实例
3. 校验返回值是否可调用
4. 单个拦截器失败不影响其他拦截器

拦截器执行顺序：OAuth 拦截器在前，自定义拦截器按声明顺序依次执行。

#### 步骤 6: 工具发现 + 同步包装

```python
client = MultiServerMCPClient(servers_config, tool_interceptors=tool_interceptors, tool_name_prefix=True)
tools = await client.get_tools()
```

`tool_name_prefix=True` 使工具名添加服务器名前缀，避免不同服务器的同名工具冲突。例如：
- 服务器 `filesystem` 的 `read_file` 工具 → `filesystem__read_file`
- 服务器 `remote-api` 的 `read_file` 工具 → `remote-api__read_file`

```python
for tool in tools:
    if getattr(tool, "func", None) is None and getattr(tool, "coroutine", None) is not None:
        tool.func = make_sync_tool_wrapper(tool.coroutine, tool.name)
```

为只有 `coroutine`（异步实现）没有 `func`（同步实现）的工具添加同步包装。这是因为 DeerFlow 的嵌入式客户端（DeerFlowClient）在同步上下文中使用工具。

## 错误处理

整个 `get_mcp_tools()` 函数被 `try/except Exception` 包裹。任何加载失败都：
1. 记录完整的错误日志（`exc_info=True`）
2. 返回空列表

这种设计确保 MCP 不可用时 Agent 的其他功能（内置工具、沙箱等）仍能正常工作。

## 设计决策

### 为什么每次都创建新的 MultiServerMCPClient

`get_mcp_tools()` 每次被调用都创建新的客户端实例。这是因为：
- 缓存在 `cache.py` 层管理，`get_mcp_tools()` 本身只在初始化时调用
- 每次使用新实例可以避免旧连接的状态泄漏
- `MultiServerMCPClient` 的生命周期管理由 langchain-mcp-adapters 负责

### 为什么 OAuth 初始注入和拦截器都要

初始注入和拦截器作用于不同阶段：
- **初始注入**: 在 `MultiServerMCPClient` 构造时生效，用于连接建立和工具发现
- **拦截器**: 在后续每次工具调用时生效，处理令牌过期和自动刷新

两者配合实现完整的 OAuth 覆盖。
