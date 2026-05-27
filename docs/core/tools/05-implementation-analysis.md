# 05 - 工具系统实现机制深度分析

> 本文档基于 `backend/packages/harness/deerflow/tools/` 目录下的源码，逐层拆解工具系统的实现细节。回答的是"代码怎么写的、为什么这么写"。

---

## 一、模块依赖关系图

```
┌──────────────────────────────────────────────────────────────────────┐
│                       调用方（外部世界）                               │
│                                                                      │
│  lead_agent/agent.py          factory.py           subagents/        │
│  ┌───────────────────────┐   ┌───────────────────┐  ┌──────────────┐ │
│  │ make_lead_agent()     │   │ create_deerflow_  │  │ task_tool    │ │
│  │  └─ get_available_    │   │ agent()           │  │ (递归入口)    │ │
│  │     tools()           │   │  └─ get_available_ │ └──────┬───────┘ │
│  └───────────┬───────────┘   │     tools()       │         │         │
└──────────────┼───────────────└─────────┬─────────┘         │         │
               │                         │                   │         │
               └─────────────┬───────────┘───────────────────┘         │
                             ▼                                         │
┌──────────────────────────────────────────────────────────────────────┐
│                     tools 包（内部世界）                              │
│                                                                      │
│  __init__.py ─── 公开入口 + skill_manage_tool 延迟导入                │
│                                                                      │
│  ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐  │
│  │ tools.py         │   │ sync.py          │   │ types.py         │  │
│  │                  │   │                  │   │                  │  │
│  │ get_available_   │   │ make_sync_tool_  │   │ Runtime = Tool   │  │
│  │ tools()          │   │ wrapper()        │   │ Runtime[dict,    │  │
│  │ 五步装配管线      │   │ 异步→同步桥接     │   │ ThreadState]     │  │
│  └────────┬─────────┘   └──────────────────┘   └──────────────────┘  │
│           │                                                          │
│  ┌────────▼──────────────────────────────────────────────────────┐   │
│  │ builtins/                                                     │   │
│  │  present_file_tool.py ─── 输出文件展示                         │   │
│  │  clarification_tool.py ─── 澄清请求（占位符）                  │   │
│  │  view_image_tool.py ─── 图片→base64                           │   │
│  │  task_tool.py ─── 子代理任务委派                               │   │
│  │  setup_agent_tool.py ─── 引导创建代理                          │   │
│  │  update_agent_tool.py ─── 自更新代理配置                       │   │
│  │  tool_search.py ─── 延迟工具搜索                               │   │
│  │  invoke_acp_agent_tool.py ─── ACP 代理调用                     │  │
│  └───────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  skill_manage_tool.py ─── 技能自演化管理                              │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ 外部依赖                                                      │    │
│  │  config/app_config.py → tools 配置                            │   │
│  │  reflection/ → resolve_variable()                             │   │
│  │  sandbox/security.py → is_host_bash_allowed()                 │   │
│  │  mcp/cache.py → get_cached_mcp_tools()                        │   │
│  └──────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 二、第 1 层：工具解析链 — config.yaml 到 Tool 实例

### 2.1 resolve_variable 调用链

```
config.yaml:
  tools:
    - name: web_search
      use: "deerflow.community.tavily:tavily_search"
      group: web

                    ↓ get_available_tools()

tool_configs = config.tools                    # [ToolConfig(name, use, group)]
  ↓ 过滤 groups
  ↓ 过滤 host-bash

loaded_tools_raw = [
  (cfg, resolve_variable(cfg.use, BaseTool))  # 反射加载
]
  ↓ resolve_variable("deerflow.community.tavily:tavily_search")

module = importlib.import_module("deerflow.community.tavily")
tool_instance = getattr(module, "tavily_search")   # BaseTool 实例
```

**名称不匹配检测**：`config.yaml` 中的 `name` 字段和工具自身的 `.name` 属性可能不一致。`get_available_tools()` 检测这种不匹配并记录警告（issue #1803 的根本原因——LLM 在 schema 中看到一个名称，运行时路由器识别另一个名称）。

---

## 三、第 2 层：五步装配管线

### 3.1 `get_available_tools()` 完整流程

```
输入：groups, include_mcp, model_name, subagent_enabled, app_config
      ↓
┌─────────────────────────────────────────────────────┐
│ 第一步：从配置文件加载工具                              │
│   config.tools → 按 groups 过滤                       │
│   → 排除 host-bash (LocalSandboxProvider 活跃时)      │
│   → resolve_variable(cfg.use) → Tool 实例             │
│   → 名称不匹配检测 + 警告                              │
│   → _ensure_sync_invocable_tool()                     │
└──────────────────────────┬──────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────┐
│ 第二步：条件性添加内置工具                              │
│   BUILTIN_TOOLS = [present_files, ask_clarification]  │
│   + skill_manage (skill_evolution 启用时)             │
│   + task (subagent_enabled 时)                       │
│   + view_image (supports_vision 时)                  │
│   + tool_search (tool_search 启用 + MCP 工具存在时)   │
└──────────────────────────┬──────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────┐
│ 第三步：加载 MCP 工具（从缓存）                        │
│   ExtensionsConfig.from_file() → 始终从磁盘读取      │
│   get_cached_mcp_tools() → mtime 缓存机制             │
│   tool_search 启用时 → 注册到 DeferredToolRegistry    │
└──────────────────────────┬──────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────┐
│ 第四步：加载 ACP 代理工具                              │
│   get_acp_agents() → 配置了 ACP 代理时                │
│   build_invoke_acp_agent_tool() → 动态构建            │
└──────────────────────────┬──────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────┐
│ 第五步：合并去重                                       │
│   all = config + builtins + MCP + ACP                │
│   按 tool.name 去重，高优先级优先保留                  │
│   重复时记录警告（issue #1803）                        │
└─────────────────────────────────────────────────────┘
```

### 3.2 优先级规则

去重时高优先级优先保留：

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1（最高） | config.yaml 工具 | 用户显式配置的工具 |
| 2 | 内置工具 | present_files, ask_clarification 等 |
| 3 | MCP 工具 | 外部 MCP 服务器提供的工具 |
| 4（最低） | ACP 工具 | 外部 ACP 代理工具 |

这意味着用户可以通过 config.yaml 中的同名工具覆盖内置工具的行为。

---

## 四、第 3 层：工具同步机制

### 4.1 `make_sync_tool_wrapper()` 异步→同步桥接

```
异步工具（如 MCP 工具）
  └─ 只有 coroutine，没有 func
      ↓ _ensure_sync_invocable_tool() 检测
      ↓ tool.func is None and tool.coroutine is not None
      ↓
make_sync_tool_wrapper(tool.coroutine, tool.name)
  │
  ├─ 检测 RunnableConfig 参数
  │   get_type_hints(func) → 找到 RunnableConfig 类型的参数
  │
  └─ 返回 sync_wrapper(*args, **kwargs)
      │
      ├─ 检测事件循环
      │   asyncio.get_running_loop()
      │
      ├─ 循环正在运行 → ThreadPoolExecutor
      │   context = contextvars.copy_context()
      │   future = executor.submit(context.run, lambda: asyncio.run(coro()))
      │   return future.result()  # 阻塞等待
      │
      └─ 循环未运行 → 直接 asyncio.run()
          return asyncio.run(coro())
```

**为什么需要 contextvars.copy_context()**：DeferredToolRegistry 使用 ContextVar 存储当前请求的注册表实例。如果不复制上下文，工作线程中 ContextVar 为空，`get_deferred_registry()` 返回 None，导致工具搜索功能失效。

---

## 五、第 4 层：内置工具实现分析

### 5.1 `present_files` — 输出文件展示

```python
@tool("present_files", parse_docstring=True)
def present_file_tool(runtime, filepaths, tool_call_id) -> Command:
    # ① 路径规范化（虚拟路径 / 宿主路径 → 统一虚拟路径）
    normalized = [_normalize_presented_filepath(runtime, f) for f in filepaths]
    # ② 安全校验（只在 /mnt/user-data/outputs/ 下）
    actual_path.relative_to(outputs_dir)  # ValueError = 路径遍历攻击
    # ③ 返回 Command 更新 artifacts 状态
    return Command(update={"artifacts": normalized, "messages": [...]})
```

`merge_artifacts` reducer 处理去重和合并，支持并行调用。

### 5.2 `ask_clarification` — 澄清请求占位符

```python
@tool("ask_clarification", parse_docstring=True, return_direct=True)
def ask_clarification_tool(question, clarification_type, context, options) -> str:
    return "Clarification request processed by middleware"
```

**这是占位符实现**：`ClarificationMiddleware.wrap_tool_call()` 拦截 `ask_clarification` 工具调用，阻止其执行，改为返回 `Command(goto=END)` 中断图执行。工具本身只提供 Schema 定义和 docstring 描述。

### 5.3 `view_image` — 图片→base64

```python
@tool("view_image", parse_docstring=True)
def view_image_tool(runtime, image_path, tool_call_id) -> Command:
    # ① 路径校验（仅 /mnt/user-data/{workspace,uploads,outputs}）
    # ② 沙箱路径验证
    # ③ 扩展名检查（jpg/jpeg/png/webp）
    # ④ 大小限制（20MB）
    # ⑤ 文件头魔数验证（防止伪装扩展名）
    # ⑥ base64 编码
    # ⑦ 返回 Command 更新 viewed_images 状态
    return Command(update={"viewed_images": {image_path: {"base64": ..., "mime_type": ...}}})
```

**双重 MIME 检测**：扩展名映射预期 MIME + 文件头魔数验证实际格式。两者必须匹配，否则返回错误。这防止 `.txt` 文件改名为 `.png` 绕过检查。

### 5.4 `setup_agent` — 引导创建代理

仅在 `is_bootstrap=True` 时绑定。写入 `{base}/users/{user_id}/agents/{agent_name}/{config.yaml,SOUL.md}`。创建失败时自动清理新目录。

### 5.5 `update_agent` — 自更新代理配置

仅在 `agent_name` 已设置时绑定（自定义 Agent 会话中）。**原子写入**：两阶段提交——所有文件先写入 `.tmp` 临时文件，全部成功后 `Path.replace` 原子重命名。

---

## 六、第 5 层：Runtime 类型设计

**文件**：`tools/types.py`

```python
Runtime = ToolRuntime[dict[str, Any], ThreadState]
```

**为什么 ContextT 用 `dict[str, Any]` 而非 TypeVar**：LangChain 调用 `model_dump()` 序列化工具的自动生成 `args_schema` 时，无界 TypeVar 导致 Pydantic 产生 `PydanticSerializationUnexpectedValue` 警告。固定为 `dict[str, Any]` 消除警告，同时保持足够灵活。

**StateT 绑定 ThreadState**：提供对线程状态（sandbox、thread_data、viewed_images 等）的类型安全访问。工具通过 `runtime.state.get("sandbox")` 获取沙箱状态。

---

## 七、文件职责速查表

| 文件 | 核心职责 | 关键类/函数 |
|------|----------|------------|
| `__init__.py` | 公开入口 + 延迟导入 | `get_available_tools`, `skill_manage_tool` |
| `tools.py` | 五步装配管线 | `get_available_tools()`, `_is_host_bash_tool()` |
| `sync.py` | 异步→同步桥接 | `make_sync_tool_wrapper()`, `_SYNC_TOOL_EXECUTOR` |
| `types.py` | Runtime 类型定义 | `Runtime = ToolRuntime[dict, ThreadState]` |
| `builtins/present_file_tool.py` | 输出文件展示 | `present_file_tool()`, `_normalize_presented_filepath()` |
| `builtins/clarification_tool.py` | 澄清请求占位符 | `ask_clarification_tool()` |
| `builtins/view_image_tool.py` | 图片→base64 | `view_image_tool()`, `_detect_image_mime()` |
| `builtins/task_tool.py` | 子代理任务委派 | `task_tool()`, `_merge_skill_allowlists()` |
| `builtins/setup_agent_tool.py` | 引导创建代理 | `setup_agent()` |
| `builtins/update_agent_tool.py` | 自更新代理配置 | `update_agent()`, `_stage_temp()` |
| `builtins/tool_search.py` | 延迟工具搜索 | `DeferredToolRegistry`, `tool_search` |
| `skill_manage_tool.py` | 技能自演化管理 | `skill_manage_tool` |
