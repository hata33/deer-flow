# 工具生命周期详解

> 涉及模块：`deerflow.reflection`、`deerflow.tools`、`deerflow.community.*`

本文档描述社区工具从配置到执行的完整生命周期，涵盖搜索工具、网页工具和 AIO 沙箱三条不同的生命周期路径。

---

## 全局生命周期概览

```
配置阶段                       加载阶段                     运行时阶段
───────────                   ──────────                   ──────────
config.yaml                   get_available_tools()        Agent 执行
    │                              │                           │
    ├─ tools: [...]               ├─ resolve_variable()       ├─ LLM 选择工具
    ├─ sandbox: {...}             ├─ ImportError? 跳过        ├─ ToolNode 调用
    └─ web_search: {...}         └─ BaseTool 实例             └─ 结果返回 Agent
```

---

## 1. 搜索/网页工具生命周期

### 阶段 1：配置发现

```yaml
# config.yaml
tools:
  - name: web_search
    use: deerflow.community.tavily.tools:web_search_tool
    group: search
  - name: web_fetch
    use: deerflow.community.jina_ai.tools:web_fetch_tool
    group: search

# 工具参数配置
web_search:
  api_key: $TAVILY_API_KEY
  max_results: 5
web_fetch:
  timeout: 15
```

每个工具配置项包含：
- `name`：配置名称（用于日志和调试）
- `use`：模块路径 + 变量名（格式：`module.path:variable_name`）
- `group`：工具分组（用于过滤，如只加载 `search` 组）

### 阶段 2：工具解析

```python
# deerflow/tools/tools.py — get_available_tools()
for cfg in tool_configs:
    tool = resolve_variable(cfg.use, BaseTool)
```

**resolve_variable 解析流程**：

```
"deerflow.community.tavily.tools:web_search_tool"
        │
        ▼ 拆分为 (module_path, var_name)
("deerflow.community.tavily.tools", "web_search_tool")
        │
        ▼ importlib.import_module()
导入 community/tavily/tools.py 模块
        │
        ▼ getattr(module, "web_search_tool")
获取 @tool 装饰器注册的 BaseTool 实例
        │
        ▼ isinstance(tool, BaseTool) 检查
类型验证通过
```

**失败路径**：

| 失败原因 | 行为 |
|:---------|:-----|
| 模块不存在 | `ImportError` → 安装提示（如 `uv add langchain-openai`） |
| 变量不存在 | `AttributeError` → 抛出异常 |
| 类型不匹配 | 跳过并记录警告 |

### 阶段 3：工具注册

```python
# 去重合并：config 工具 > 内置工具 > MCP 工具 > ACP 工具
unique_tools = []
seen_names = set()
for tool in all_tools:
    if tool.name not in seen_names:
        unique_tools.append(tool)
        seen_names.add(tool.name)
```

同名工具的去重策略保证 `config.yaml` 中先声明的工具优先级最高。

### 阶段 4：Agent 调用

```
用户输入 → Lead Agent → LLM → 选择 web_search 工具
                                    │
                                    ▼
                            ToolNode 调用 web_search_tool.invoke({"query": "..."})
                                    │
                                    ▼
                            工具内部：
                            1. get_app_config().get_tool_config("web_search")
                               → 读取 API Key、max_results 等配置
                            2. 创建 API 客户端（TavilyClient / DDGS / ...）
                            3. 执行搜索请求
                            4. 标准化结果为 JSON
                            5. 返回 JSON 字符串给 Agent
                                    │
                                    ▼
                            LLM 处理搜索结果，生成回复
```

**关键点**：工具的 API Key 和参数配置**不在工具注册时读取**，而是在**每次调用时**从 `get_app_config()` 动态获取。这确保运行时修改 `config.yaml` 后立即生效（配合 config mtime 自动重载）。

### 阶段 5：异步工具适配

```python
# tools/tools.py — _ensure_sync_invocable_tool()
if tool.func is None and tool.coroutine is not None:
    tool.func = make_sync_tool_wrapper(tool.coroutine, tool.name)
```

对于 Jina AI 等异步工具（`async def`），自动生成同步包装器，确保嵌入式客户端（`DeerFlowClient`）在同步上下文中也能调用。

---

## 2. AIO 沙箱生命周期

### 阶段 1：配置检测

```yaml
# config.yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  image: enterprise-public-cn-beijing.cr.volces.com/.../all-in-one-sandbox:latest
  port: 8080
  idle_timeout: 600
  replicas: 3
```

`SandboxMiddleware` 在首次请求时创建 `AioSandboxProvider` 单例：

```python
# deerflow/agents/middlewares/sandbox_middleware.py
provider = resolve_class(config.sandbox.use, SandboxProvider)
sandbox_provider = provider()  # AioSandboxProvider()
```

### 阶段 2：Provider 初始化

```
AioSandboxProvider.__init__()
        │
        ├── _load_config()          # 从 config.yaml 读取沙箱配置
        ├── _create_backend()       # 选择 LocalContainerBackend 或 RemoteSandboxBackend
        ├── _reconcile_orphans()    # 扫描并收养孤儿容器
        ├── _start_idle_checker()   # 启动空闲检查守护线程
        └── atexit.register(shutdown)  # 注册退出清理
```

### 阶段 3：容器获取（acquire）

```
SandboxMiddleware → provider.acquire(thread_id)
        │
        ▼
┌─ Layer 1: 进程内缓存 ──────────────────────────┐
│  _thread_sandboxes[thread_id] → sandbox_id?     │
│  命中 → 更新 last_activity → 返回               │
└─────────────────────────────────────────────────┘
        │ 未命中
        ▼
┌─ Layer 1.5: 暖池 ─────────────────────────────┐
│  _warm_pool[sandbox_id]?                        │
│  命中 → 弹出 → 创建 AioSandbox → 缓存 → 返回   │
└─────────────────────────────────────────────────┘
        │ 未命中
        ▼
┌─ Layer 2: 文件锁保护的后端发现 ────────────────┐
│  lock_path = {thread_dir}/{sandbox_id}.lock     │
│  ┌─ 文件锁内 ──────────────────────────────────┐│
│  │ 1. 重新检查进程缓存（双检锁）               ││
│  │ 2. 重新检查暖池                             ││
│  │ 3. backend.discover(sandbox_id)              ││
│  │    命中 → 创建 AioSandbox → 缓存 → 返回     ││
│  │ 4. backend.create(thread_id, sandbox_id)      ││
│  │    → 启动新容器 / 调用 Provisioner            ││
│  │    → wait_for_sandbox_ready(url, timeout=60) ││
│  │    → 创建 AioSandbox → 缓存 → 返回           ││
│  └──────────────────────────────────────────────┘│
└─────────────────────────────────────────────────┘
```

### 阶段 4：容器使用

```
Agent 调用 bash/ls/read_file/write_file/str_replace 工具
        │
        ▼
tools.py → sandbox.execute_command("ls /mnt/user-data/workspace")
        │
        ▼
AioSandbox.execute_command(command)
        │
        ├── threading.Lock() 获取锁
        ├── client.shell.exec_command(command, no_change_timeout=600)
        ├── 检查 ErrorObservation → 自动重试
        └── 返回输出字符串
```

### 阶段 5：容器释放（release）

```
SandboxMiddleware → provider.release(sandbox_id)
        │
        ├── 从 _sandboxes 移除（不再活跃）
        ├── 从 _thread_sandboxes 清除映射
        ├── 从 _last_activity 移除
        └── 放入 _warm_pool（容器继续运行）
                │
                ▼ 后续 acquire 可从暖池复用（避免冷启动）
                或由空闲检查器在 idle_timeout 后销毁
```

### 阶段 6：容器销毁（destroy）

```
显式销毁 / 空闲检查器 / shutdown()
        │
        ├── 从所有内部字典中移除
        └── backend.destroy(info)
                │
                ├── LocalContainerBackend:
                │   docker stop {container_id}  # --rm 自动删除
                │   release_port(port)
                │
                └── RemoteSandboxBackend:
                    DELETE /api/sandboxes/{sandbox_id}
```

---

## 3. 可选依赖降级流程

```
config.yaml 声明工具
        │
        ▼
get_available_tools() 遍历 tool_configs
        │
        ▼
resolve_variable("deerflow.community.tavily.tools:web_search_tool")
        │
        ├── importlib.import_module() 成功
        │       │
        │       ▼
        │   getattr() 获取 BaseTool 实例
        │       │
        │       ▼
        │   工具注册成功，加入 available_tools
        │
        └── importlib.import_module() 失败（ImportError）
                │
                ▼
            生成安装提示消息（如 "uv add tavily-python"）
                │
                ▼
            抛出异常 → get_available_tools() 捕获 → 跳过该工具
```

**双层防护**：

1. **模块级**：`resolve_variable` 导入失败时工具不会注册
2. **函数级**：工具内部（如 `ddg_search`）使用 `try/except ImportError` 保护第三方库导入

---

## 4. 配置热更新

DeerFlow 的配置系统支持**热更新**，无需重启服务：

1. **Config 缓存重载**：`get_app_config()` 检测 `config.yaml` 的 mtime 变化，自动重新解析
2. **工具参数即时生效**：每次工具调用都从 `get_app_config()` 读取最新配置
3. **MCP 工具刷新**：`get_cached_mcp_tools()` 检测 `extensions_config.json` 的 mtime 变化

**注意事项**：
- 工具的**注册**（哪些工具可用）需要重新创建 Agent 才能生效
- 工具的**参数**（API Key、max_results 等）可以即时生效
- 沙箱配置（image、replicas 等）需要重启 `AioSandboxProvider` 才能生效

---

## 5. 关键时序参数汇总

| 参数 | 默认值 | 来源 | 说明 |
|:-----|:-------|:-----|:-----|
| `idle_timeout` | 600s | config.yaml | 沙箱空闲销毁超时 |
| `replicas` | 3 | config.yaml | 最大并发容器数 |
| `IDLE_CHECK_INTERVAL` | 60s | 常量 | 空闲检查轮询间隔 |
| `wait_for_sandbox_ready` | 30s | 函数参数 | 沙箱启动等待超时 |
| `_DEFAULT_NO_CHANGE_TIMEOUT` | 600s | 常量 | 命令执行无输出超时 |
| `_MAX_DOWNLOAD_SIZE` | 100MB | 常量 | 文件下载大小上限 |
| 内容截断 | 4096 字符 | 工具代码 | web_fetch 输出截断 |
| 搜索结果数 | 5 | config.yaml | 默认搜索结果数量 |
| DDG 客户端超时 | 30s | 代码 | DuckDuckGo 请求超时 |
| Jina 请求超时 | 10s | config.yaml | Jina API 请求超时 |
| 端口重试 | 10 次 | 代码 | 端口冲突重试次数 |
| 容器启动超时 | 60s | 代码 | 新容器就绪等待超时 |
