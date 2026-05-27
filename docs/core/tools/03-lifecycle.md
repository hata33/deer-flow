# 工具完整生命周期

本文档描述一个工具从注册到执行的完整生命周期，涵盖工具注册、装配、访问和执行的各个阶段。

## 1. 工具注册

### 1.1 配置文件注册

工具在 `config.yaml` 中定义：

```yaml
tools:
  - name: read_file
    use: deerflow.sandbox.tools:read_file_tool
    group: filesystem
```

每个工具条目包含：
- `name`：工具的显示名称
- `use`：实现引用路径（`module.path:attribute` 格式）
- `group`：可选的分组标签

### 1.2 配置解析

应用启动时，`AppConfig` 从 `config.yaml` 加载工具配置列表。每个工具配置是一个 `ToolConfig` 对象，包含 `name`、`use`、`group` 等字段。

### 1.3 实例化

在 `get_available_tools()` 中，通过 `resolve_variable()` 将 `use` 字符串动态解析为实际的 `BaseTool` 实例：

```python
loaded_tools_raw = [
    (cfg, resolve_variable(cfg.use, BaseTool))
    for cfg in tool_configs
]
```

`resolve_variable` 会导入指定模块并获取指定属性，返回一个 `BaseTool` 实例。

## 2. 工具装配

### 2.1 装配管线

`get_available_tools()` 按以下顺序执行装配：

```
┌─────────────────────────────────────────────────┐
│ 1. 加载配置工具                                   │
│    config.yaml → filter by groups                │
│    → filter host-bash                            │
│    → resolve_variable → Tool instances           │
│    → name mismatch warning                       │
│    → _ensure_sync_invocable_tool                 │
├─────────────────────────────────────────────────┤
│ 2. 添加内置工具                                   │
│    BUILTIN_TOOLS (present_files, ask_clarification)│
│    + skill_manage (if skill_evolution enabled)   │
│    + SUBAGENT_TOOLS (if subagent_enabled)        │
│    + view_image (if model supports_vision)       │
│    + tool_search (if tool_search.enabled)        │
├─────────────────────────────────────────────────┤
│ 3. 加载 MCP 工具                                 │
│    ExtensionsConfig.from_file()                   │
│    → get_cached_mcp_tools()                       │
│    → register in DeferredToolRegistry (optional) │
├─────────────────────────────────────────────────┤
│ 4. 加载 ACP 工具                                 │
│    get_acp_agents()                               │
│    → build_invoke_acp_agent_tool(agents)          │
├─────────────────────────────────────────────────┤
│ 5. 合并去重                                      │
│    config > builtins > MCP > ACP                  │
│    → deduplicate by name                          │
│    → _ensure_sync_invocable_tool for all          │
└─────────────────────────────────────────────────┘
```

### 2.2 安全过滤

在装配过程中，以下安全检查会执行：

1. **Host-bash 过滤**：当使用 `LocalSandboxProvider` 时，host-bash 工具被过滤
2. **名称不匹配警告**：配置名称与工具 `.name` 属性不匹配时记录警告
3. **去重**：相同名称的工具只保留高优先级的

### 2.3 同步包装

所有工具通过 `_ensure_sync_invocable_tool()` 检查：如果只有 `coroutine` 没有 `func`，自动生成同步包装器。

## 3. 运行时工具访问

### 3.1 从 ThreadState 到工具

```
用户请求 → LangGraph 图执行
    → Lead Agent 节点
    → get_available_tools() 获取工具列表
    → bind_tools() 绑定到 LLM
    → LLM 生成工具调用
    → ToolNode 执行工具
```

### 3.2 Runtime 传递

每个工具接收一个 `Runtime` 类型的参数，包含：

```python
Runtime = ToolRuntime[dict[str, Any], ThreadState]
```

- `runtime.context`：上下文字典（包含 thread_id、agent_name、app_config 等）
- `runtime.state`：ThreadState（包含 sandbox、thread_data 等）
- `runtime.config`：RunnableConfig（包含 configurable、metadata 等）

### 3.3 工具调用

工具通过 LangGraph 的 ToolNode 执行：

1. LLM 生成工具调用请求（ToolCall）
2. ToolNode 查找匹配的工具
3. 调用工具的 `func`（同步）或 `coroutine`（异步）
4. 工具返回结果

## 4. MCP 工具延迟加载

### 4.1 启动时初始化

```
应用启动
    → initialize_mcp_tools()
    → 连接 MCP 服务器
    → 发现工具
    → 缓存到 MCP 工具缓存
```

### 4.2 延迟注册

当 `tool_search.enabled` 为 True 时：

```
get_available_tools()
    → 获取缓存的 MCP 工具
    → 创建 DeferredToolRegistry
    → 所有 MCP 工具注册为延迟工具
    → tool_search 添加到内置工具列表
    → DeferredToolFilterMiddleware 过滤延迟工具
```

### 4.3 运行时搜索

```
代理看到 <available-deferred-tools> 中的工具名
    → 调用 tool_search("select:Read,Edit")
    → DeferredToolRegistry.search() 查找匹配
    → 返回完整 schema（OpenAI function 格式）
    → promote() 从注册表移除
    → DeferredToolFilterMiddleware 不再过滤
    → 工具在后续调用中可用
```

### 4.4 重入保护

当子代理生成时，`get_available_tools()` 会被重入调用。此时复用父代理的注册表：

```python
existing_registry = get_deferred_registry()
if existing_registry is None:
    # 首次：创建新注册表
    registry = DeferredToolRegistry()
    for t in mcp_tools:
        registry.register(t)
    set_deferred_registry(registry)
else:
    # 重入：保留已提升的工具（issue #2884 修复）
    pass
```

## 5. ACP 代理工具调用流程

（Agent Client Protocol，代理-客户端协议）它的核心价值：标准化与“一次接入，处处可用”

### 5.1 工具构建

```
get_available_tools()
    → get_acp_agents() 读取 ACP 配置
    → build_invoke_acp_agent_tool(agents)
    → 动态生成工具描述（包含可用代理列表）
    → 返回 StructuredTool（coroutine=_invoke）
```

### 5.2 调用执行

```
LLM 生成 invoke_acp_agent 调用
    → _invoke() 执行
    → 验证代理名称
    → 获取每线程工作空间（_get_work_dir）
    → 构建 MCP 服务器配置（_build_acp_mcp_servers）
    → spawn_agent_process 启动 ACP 代理进程
    → initialize 协议握手
    → new_session 创建会话
    → prompt 发送任务
    → _CollectingClient 收集流式响应
    → 返回最终文本
```

### 5.3 每线程工作空间

```
{base_dir}/threads/{thread_id}/acp-workspace/
```

每个线程获得独立的工作空间，确保并发会话之间互不干扰。

## 6. 子代理工具调用流程

### 6.1 任务启动

```
LLM 生成 task 调用
    → task_tool() 执行
    → 解析子代理配置
    → get_available_tools(subagent_enabled=False) 获取子代理工具
    → SubagentExecutor 创建执行器
    → execute_async() 启动后台线程执行
```

### 6.2 轮询等待

```
while True:
    → get_background_task_result() 获取状态
    → 检查新消息 → 发送 task_running 事件
    → 检查终态 → 返回结果 / 继续轮询
    → await asyncio.sleep(5)
    → 轮询超时检查
```

### 6.3 取消处理

```
asyncio.CancelledError
    → request_cancel_background_task() 请求停止
    → asyncio.shield(_await_subagent_terminal()) 等待终态
    → _report_subagent_usage() 报告令牌使用量
    → cleanup_background_task() 或 _schedule_deferred_subagent_cleanup()
    → re-raise CancelledError
```

## 7. 状态更新模式

许多内置工具使用 LangGraph 的 `Command` 对象更新状态：

```python
return Command(
    update={
        "artifacts": normalized_paths,        # 由 merge_artifacts reducer 处理
        "messages": [ToolMessage(...)],        # 追加到消息列表
        "viewed_images": {...},                # 由 merge_viewed_images reducer 处理
        "created_agent_name": agent_name,      # 直接更新
    }
)
```

状态更新通过 reducer 函数处理，确保并发操作不会产生冲突。
