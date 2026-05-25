# 工具系统全局概览

DeerFlow 的工具系统负责管理和装配所有可供代理调用的工具。工具系统通过一条明确的装配管线（Assembly Pipeline），按优先级顺序收集、过滤、去重工具，最终生成代理可用的工具列表。

## 工具装配管线

`get_available_tools()` 是工具系统的核心入口，按以下优先级顺序装配工具：

```
配置工具（config.yaml）→ 内置工具（builtins）→ MCP 工具 → ACP 工具
```

### 1. 配置工具（Config Tools）

从 `config.yaml` 的 `tools` 字段加载。每个工具条目包含：

- `name`：工具名称
- `use`：工具实现的引用路径（如 `module.path:tool_name`）
- `group`：可选的工具组分类

```yaml
tools:
  - name: read_file
    use: deerflow.sandbox.tools:read_file_tool
    group: filesystem
```

通过 `resolve_variable()` 将 `use` 字符串解析为实际的 `BaseTool` 实例。

**安全过滤**：当使用 `LocalSandboxProvider` 时，host-bash 工具会被自动过滤掉。

### 2. 内置工具（Builtin Tools）

始终包含的核心工具：

| 工具 | 功能 | 加载条件 |
|------|------|----------|
| `present_files` | 展示输出文件给用户 | 始终 |
| `ask_clarification` | 向用户请求澄清 | 始终 |
| `setup_agent` | 创建自定义代理 | 始终 |
| `update_agent` | 更新自定义代理 | 始终 |
| `view_image` | 读取图片文件 | 模型 supports_vision=True |
| `task` | 委派任务给子代理 | subagent_enabled=True |
| `skill_manage` | 管理自定义技能 | skill_evolution.enabled=True |
| `tool_search` | 搜索延迟加载的工具 | tool_search.enabled=True 且有 MCP 工具 |

### 3. MCP 工具（MCP Tools）

通过 MCP（Model Context Protocol）服务器提供的工具。启动时通过 `initialize_mcp_tools()` 初始化并缓存。

特点：
- 配置始终从磁盘重新读取（`ExtensionsConfig.from_file()`），确保 Gateway API 的更改立即生效
- 支持 tool_search 延迟注册机制

### 4. ACP 工具（ACP Tools）

通过 ACP（Agent Client Protocol）兼容的外部代理提供的工具。仅当配置了 ACP 代理时才注册。

- `invoke_acp_agent`：动态构建，描述中包含所有可用代理列表

## 工具组与过滤

工具可以通过 `group` 字段分类，`get_available_tools()` 支持 `groups` 参数按组过滤：

```python
# 只加载 filesystem 组的工具
tools = get_available_tools(groups=["filesystem"])
```

## 延迟加载（tool_search）

当配置了 `tool_search.enabled: true` 时，MCP 工具不会一次性全部加载到模型上下文中，而是：

1. 所有 MCP 工具注册到 `DeferredToolRegistry`
2. 工具名称出现在系统提示的 `<available-deferred-tools>` 中
3. 代理通过 `tool_search` 工具按需搜索和加载工具 schema
4. 已加载的工具从注册表中"提升"（promote），后续调用不再被过滤

延迟加载的好处：
- 减少上下文占用
- 按需加载，提高效率
- 减少每次模型调用的 token 数量

## 自定义工具注册

通过 `config.yaml` 注册自定义工具：

```yaml
tools:
  - name: my_custom_tool
    use: my_package.tools:my_tool
    group: custom
```

`use` 字段格式为 `module.path:attribute_name`，`resolve_variable()` 会动态导入并解析。

## 模块结构

```
deerflow/tools/
├── __init__.py              # 包入口，延迟导入 skill_manage_tool
├── tools.py                 # 工具装配管线（get_available_tools）
├── sync.py                  # 异步→同步桥接包装器
├── types.py                 # Runtime 类型别名
├── skill_manage_tool.py     # 自定义技能管理工具
└── builtins/
    ├── __init__.py          # 内置工具导出
    ├── clarification_tool.py    # ask_clarification 工具
    ├── present_file_tool.py     # present_files 工具
    ├── task_tool.py             # task 子代理委派工具
    ├── tool_search.py           # tool_search 延迟加载工具
    ├── view_image_tool.py       # view_image 图片查看工具
    ├── setup_agent_tool.py      # setup_agent 代理创建工具
    ├── update_agent_tool.py     # update_agent 代理更新工具
    └── invoke_acp_agent_tool.py # invoke_acp_agent ACP 调用工具
```

## 去重策略

所有工具按装配顺序合并后，按工具名去重。高优先级的工具优先保留：

```
配置工具 > 内置工具 > MCP 工具 > ACP 工具
```

重复名称的工具会被跳过并记录警告日志（关联 issue #1803）。

## 同步包装

所有通过 `_ensure_sync_invocable_tool()` 处理的工具都会被检查：如果工具只定义了 `coroutine` 而没有 `func`，则自动使用 `make_sync_tool_wrapper()` 生成同步包装器。这确保了嵌入式 DeerFlowClient 等同步调用路径也能正常工作。

详见 [02-sync.md](./02-sync.md)。
