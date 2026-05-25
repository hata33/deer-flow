# Client — 客户端配置构建

## 模块路径

`deerflow.mcp.client`

## 解决的问题

`extensions_config.json` 使用 Pydantic 模型（`McpServerConfig`）声明 MCP 服务器，但 `langchain-mcp-adapters` 的 `MultiServerMCPClient` 期望特定格式的参数字典。本模块在两者之间做适配转换。

核心映射关系：

| extensions_config.json 字段 | langchain-mcp-adapters 参数 | 说明 |
|------------------------------|------------------------------|------|
| `type` | `transport` | 传输类型（stdio/sse/http） |
| `command` + `args` | `command` + `args` | stdio 模式的启动命令和参数 |
| `env` | `env` | 传递给子进程的环境变量 |
| `url` | `url` | SSE/HTTP 模式的服务器地址 |
| `headers` | `headers` | SSE/HTTP 模式的自定义 HTTP 头 |

## 核心函数

### `build_server_params(server_name, config)` — 单服务器参数构建

根据传输类型构建不同结构的参数字典：

**stdio 传输**：
```python
{
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@mcp/server-filesystem", "/path"],
    "env": {"API_KEY": "xxx"}  # 可选
}
```

校验：`command` 字段必须存在。

**SSE/HTTP 传输**：
```python
{
    "transport": "sse",  # 或 "http"
    "url": "https://api.example.com/mcp",
    "headers": {"X-Custom": "value"}  # 可选
}
```

校验：`url` 字段必须存在。

不支持的传输类型抛出 `ValueError`。

### `build_servers_config(extensions_config)` — 批量构建

遍历所有 `enabled=true` 的服务器，逐一构建参数。**单服务器失败不影响其他服务器**——错误记录到日志，继续处理下一个。

这种容错设计的原因：MCP 服务器由不同团队/供应商提供，某个配置错误不应阻止其他正常服务器的加载。

## 配置校验

| 传输类型 | 必须字段 | 缺失行为 |
|----------|----------|----------|
| stdio | `command` | `ValueError` |
| sse | `url` | `ValueError` |
| http | `url` | `ValueError` |
| 其他 | — | `ValueError`（不支持的类型） |

## 设计决策

### 为什么不做连接测试

本模块只负责配置转换，不负责连接。连接测试在 `MultiServerMCPClient` 初始化时自动进行。将配置和连接分离的好处：
- 配置错误在构建阶段就能发现（快速失败）
- 连接错误在运行时处理（允许重试）
