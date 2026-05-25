# Cache — 工具缓存管理

## 模块路径

`deerflow.mcp.cache`

## 解决的问题

MCP 工具的初始化成本很高：
- **stdio** 传输需要启动子进程
- **SSE/HTTP** 传输需要建立网络连接
- 工具发现（`get_tools()`）需要与每个服务器进行协议握手

如果每次 Agent 调用都重新加载，性能不可接受。缓存模块解决以下三个问题：

1. **避免重复加载**：首次加载后缓存结果
2. **配置热更新**：Gateway API 修改配置后自动检测并重新加载
3. **多事件循环兼容**：支持 FastAPI 和 LangGraph Studio 两种运行环境

## 全局状态

```python
_mcp_tools_cache: list[BaseTool] | None = None  # 缓存的工具列表
_cache_initialized = False                       # 是否已初始化
_initialization_lock = asyncio.Lock()            # 初始化锁
_config_mtime: float | None = None               # 配置文件修改时间
```

所有状态是模块级全局变量，整个进程共享同一份缓存。

## 缓存失效策略 — mtime 比对

```
当前文件 mtime > 缓存记录的 mtime → 缓存过期
```

`_get_config_mtime()` 通过 `ExtensionsConfig.resolve_config_path()` 定位配置文件，使用 `os.path.getmtime()` 获取修改时间。

`_is_cache_stale()` 比对当前 mtime 与缓存时记录的 mtime：
- 文件被修改 → 过期
- 文件不存在 → 不过期（保守策略）
- 缓存未初始化 → 不过期（由初始化逻辑处理）

## 核心函数

### `initialize_mcp_tools()` — 显式初始化

在应用启动时调用。使用 `asyncio.Lock` 防止并发初始化。初始化完成后记录 `_config_mtime`。

### `get_cached_mcp_tools()` — 懒加载入口

这是获取 MCP 工具的主要入口点。执行流程：

1. **热更新检测**：调用 `_is_cache_stale()` 检查配置是否变更
2. **缓存命中**：如果已初始化且未过期，直接返回缓存
3. **懒加载**：如果未初始化，根据当前事件循环状态选择加载策略

多事件循环处理：

| 环境 | 事件循环状态 | 加载策略 |
|------|-------------|----------|
| LangGraph Studio | 循环已运行 | `ThreadPoolExecutor` + `asyncio.run`（独立线程） |
| FastAPI 启动 | 循环存在未运行 | `loop.run_until_complete` |
| 脚本/测试 | 无循环 | `asyncio.run` |

所有路径的错误都捕获为日志，返回空列表，不影响 Agent 其他功能。

### `reset_mcp_tools_cache()` — 重置缓存

将所有状态恢复为"未初始化"。用于配置热更新和测试。

## 为什么不用 TTL 缓存

mtime 比对比 TTL 更精确：
- 配置未修改时不触发重新加载（零开销）
- 配置修改后立即生效（无需等待 TTL 过期）
- 无需选择 TTL 值（太短浪费、太长不敏感）
