# 004-工具系统

## 解决什么问题

Agent 需要调用工具（bash、文件操作、搜索、子代理委派等），但工具来源多样：配置文件定义的、内置的、MCP 服务器提供的、ACP 代理注入的。
不同工具的加载条件也不同：有的按 groups 过滤，有的按模型能力（vision）决定，有的按运行时参数（subagent_enabled）注入。
本模块把所有来源的工具统一收集、过滤、组装成一个 `list[BaseTool]` 给 Agent 工厂。

## 本模块的职责边界

**只负责工具的加载和组装**：按条件从各来源收集工具实例，返回列表。
不负责：工具的具体实现（bash 执行、文件读写、MCP 协议通信）、工具调用编排（Agent Loop）、工具结果的展示（前端的事）。

## 不可变的设计决策

**反射加载工具实例**：`resolve_variable("deerflow.sandbox.tools:bash_tool")` 从配置字符串解析。
与模型工厂的 `resolve_class` 同理——新增工具只改 config.yaml，不编译代码。

**四层来源叠加而非单一来源**：配置工具（config.yaml）+ 内置工具（BUILTIN_TOOLS）+ MCP 缓存工具 + ACP 代理工具。
每层有独立的加载条件和过滤逻辑。混在一起则无法按需启用/禁用某一层。

**内置工具按条件注入**：
- `present_file_tool` + `ask_clarification_tool`：所有 Agent 必有（文件展示 + 澄清中断是基础能力）。
- `task_tool`：仅 `subagent_enabled=True` 时注入（未启用子代理时不暴露 task 工具给 LLM，避免幻觉调用）。
- `view_image_tool`：仅 `supports_vision=True` 的模型注入（非视觉模型无法处理 base64 图片）。

**groups 过滤只作用于配置工具**：内置工具、MCP 工具、ACP 工具不受 groups 过滤。
原因：内置工具是 Agent 基础能力，不应被配置裁剪；MCP/ACP 工具已有独立的启用/禁用机制。

**本地沙箱安全过滤**：`is_host_bash_allowed()` 检查时，从配置工具中移除 bash 相关工具。
本地沙箱不是安全隔离边界，直接暴露宿主机 bash 是危险的。Docker 沙箱则默认允许。

**tool_search 延迟加载模式**：MCP 工具数量可能很多（几十到上百），全部加载到 Agent 上下文会消耗 token。
启用 `tool_search` 后，MCP 工具只注册名称和描述到 `DeferredToolRegistry`，LLM 需要时通过 `tool_search` 工具查询完整参数定义。
注册后自动 `promote`——匹配到的工具从延迟列表移到活跃列表，后续 `DeferredToolFilterMiddleware` 不再过滤。

**每请求注册表（ContextVar）**：`DeferredToolRegistry` 用 `contextvars.ContextVar` 存储，非模块级全局变量。
asyncio 中每个图执行在独立异步上下文中运行，并发请求不会互相干扰。

**沙箱工具的路径安全**：虚拟路径 `/mnt/user-data/*` 到宿主机路径的映射有双重验证——`_reject_path_traversal` 拒绝 `..`，`_validate_resolved_user_data_path` 用 `relative_to` 确认不出界。
本地沙箱还需 `validate_local_tool_path` 检查路径是否在允许范围内。

**本地沙箱输出脱敏**：`mask_local_paths_in_output` 把宿主机绝对路径替换回虚拟路径。
LLM 不应看到宿主机文件系统布局（安全信息泄漏）。

**present_file_tool 路径归一化**：接受虚拟路径和宿主机路径两种输入，统一归一化为 `/mnt/user-data/outputs/*`。
通过 `Command(update={"artifacts": ...})` 更新状态，`merge_artifacts` reducer 负责去重合并。

**task_tool 防递归**：子代理加载工具时传 `subagent_enabled=False`，不再注入 `task_tool`。
否则子代理可以无限嵌套调用 task。

## 适配层

```yaml
<ADAPT>
# === 框架 ===
tool_base: "BaseTool"                      # LangChain 工具基类
tool_decorator: "@tool"                     # 工具注册装饰器
reflection_fn: "resolve_variable(path, type)" # 反射函数

# === 工具来源（按需启用）===
tool_sources:
  config_tools: true                        # 从 config.yaml 加载
  builtin_tools: true                       # 内置工具
  mcp_tools: true                           # MCP 服务器工具
  acp_tools: true                           # ACP 代理工具

# === 内置工具（按需启用）===
builtin_tools:
  - name: "present_file_tool"
    condition: "always"
  - name: "ask_clarification_tool"
    condition: "always"
  - name: "task_tool"
    condition: "subagent_enabled == true"
  - name: "view_image_tool"
    condition: "model.supports_vision == true"

# === 沙箱工具（按需启用）===
sandbox_tools:
  - "bash"
  - "ls"
  - "read_file"
  - "write_file"
  - "str_replace"

# === 延迟加载 ===
tool_search_enabled: true                   # 启用 tool_search 延迟加载
max_search_results: 5                       # 每次搜索最多返回工具数

# === 安全 ===
virtual_path_prefix: "/mnt/user-data"       # 虚拟路径前缀
host_bash_allowed_check: "is_host_bash_allowed(config)"
local_path_masking: true                    # 本地沙箱输出脱敏
</ADAPT>
```

## 自检清单

| # | 验证 | 期望 |
|---|------|------|
| 1 | groups=["bash"] | 只返回 group=bash 的配置工具 + 全部内置工具 |
| 2 | groups=None | 返回全部配置工具 + 全部内置工具 |
| 3 | subagent_enabled=false | 不包含 task_tool |
| 4 | subagent_enabled=true | 包含 task_tool |
| 5 | model 不支持 vision | 不包含 view_image_tool |
| 6 | model 支持 vision | 包含 view_image_tool |
| 7 | 本地沙箱 + 未允许 host bash | bash 工具被过滤 |
| 8 | tool_search 启用 + MCP 工具存在 | MCP 工具延迟注册 + tool_search 工具注入 |
| 9 | tool_search 查询 "select:bash" | 返回 bash 工具完整定义 + promote |
| 10 | present_file_tool 路径在 outputs 外 | ValueError |
| 11 | bash 命令含 `..` | PermissionError |
| 12 | 子代理工具列表 | 不包含 task_tool（防递归） |

## 依赖模块

| 模块 | 本模块调用的接口 |
|------|----------------|
| **配置系统** | `get_app_config()` / `config.tools` / `config.tool_search` |
| **反射系统** | `resolve_variable(tool.use, BaseTool)` |
| **沙箱系统** | `ensure_sandbox_initialized()` / 路径安全校验 |
| **MCP 系统** | `get_cached_mcp_tools()` |
| **ACP 系统** | `get_acp_agents()` / `build_invoke_acp_agent_tool()` |
| **状态模式** | `ThreadState` / `merge_artifacts` / `merge_viewed_images` |

---

## 参考源码

以下是已验证的源码实现。阅读并理解其模式，然后根据 `<ADAPT>` 适配参数在新项目中重写。
不要复制粘贴——重点关注每个函数的**决策分支**和**边界条件处理**。

### 文件清单（按阅读顺序）

| 文件 | 职责 | 阅读重点 |
|------|------|---------|
| `tool_config.py` | ToolConfig / ToolGroupConfig Pydantic 定义 | `use` 字段用于反射；`group` 字段用于过滤 |
| `tool_search_config.py` | 延迟加载开关 | 模块级单例 + `load_from_dict` 模式 |
| `tools.py` | 工厂入口 `get_available_tools` | 四层来源叠加顺序；groups 过滤只作用于配置工具；MCP 延迟注册 + promote 流程 |
| `clarification_tool.py` | 用户澄清工具 | `return_direct=True` + 被 ClarificationMiddleware 拦截 |
| `present_file_tool.py` | 文件展示工具 | 虚拟路径归一化；`Command(update={"artifacts": ...})` reducer 合并 |
| `view_image_tool.py` | 图片查看工具 | base64 编码；`Command(update={"viewed_images": ...})` reducer 合并 |
| `task_tool.py` | 子代理委派工具 | 异步后台执行 + 5秒轮询 + SSE 进度推送；`subagent_enabled=False` 防递归 |
| `tool_search.py` | 延迟工具搜索 | `DeferredToolRegistry` 三种查询模式（select/+keyword/regex）；`ContextVar` 每请求隔离 |
| `security.py` | 沙箱安全检查 | `is_host_bash_allowed` 本地沙箱需显式允许；类路径标记匹配 |
| `sandbox/tools.py` | 沙箱工具集 | bash/ls/read_file/write_file/str_replace 五个工具；本地沙箱路径校验→替换→脱敏流水线 |

源码文件见同目录下的 `src/` 子文件夹。
