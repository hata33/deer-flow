# 04 - 工具系统设计决策与考量

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

---

## 一、核心设计决策清单

| # | 决策 | 一句话动机 |
|---|------|-----------|
| 1 | **@tool 装饰器 + parse_docstring=True** | 从 docstring 自动生成 JSON Schema，减少手动维护 |
| 2 | **resolve_variable 反射解析** | 配置驱动，解耦工具注册与工具实现 |
| 3 | **工具组（tool groups）概念** | 逻辑分组，按角色过滤工具 |
| 4 | **view_image 条件绑定 supports_vision** | 非视觉模型无法处理 base64 图像 |
| 5 | **task 工具防止递归子代理调用** | 避免无限嵌套导致资源耗尽 |

---

## 二、逐决策分析

### 决策 1：@tool 装饰器 + parse_docstring=True

**问题**：LLM 需要每个工具的 JSON Schema（参数名、类型、描述）来正确调用，如何生成？

| 方案 | 优势 | 劣势 |
|------|------|------|
| `@tool(parse_docstring=True)`（当前） | docstring 即文档即 Schema；修改函数时 Schema 自动更新 | docstring 格式必须严格符合 Google/NumPy 风格 |
| 手动定义 `args_schema` Pydantic 模型 | 类型更精确，支持验证 | 重复维护（函数签名 + Schema 模型） |
| 自动推断（无 docstring） | 零额外工作 | LLM 看不到参数描述，调用准确率低 |

**选择 parse_docstring=True**：`present_files`、`ask_clarification`、`view_image`、`task` 等工具使用 `@tool("name", parse_docstring=True)` 装饰。LangChain 从函数签名提取参数名和类型注解，从 Google 风格 docstring 的 `Args:` 段提取参数描述，自动合成 `args_schema` Pydantic 模型。开发者只需维护一份 docstring，既是 LLM 看到的工具描述，也是自动生成的参数 Schema。

**双语文档**：DeerFlow 的工具 docstring 同时包含英文和中文描述。英文作为 LLM 的主要指令（模型训练语料以英文为主），中文作为开发者参考。

---

### 决策 2：resolve_variable 反射解析

**问题**：工具实现在不同模块中（sandbox、community、MCP），配置如何引用？

| 方案 | 优势 | 劣势 |
|------|------|------|
| `resolve_variable`（当前） | 配置与实现完全解耦；动态加载 | 字符串路径拼写错误仅在运行时发现 |
| 硬编码注册表 | 编译时安全 | 每添加工具需修改注册表代码 |
| 插件发现（entry_points） | 自动发现 | 需要 package 安装；不适合 YAML 配置驱动 |

**选择 resolve_variable**：`config.yaml` 中的 `tools` 字段使用 `use: "module.path:function_name"` 格式。`resolve_variable()` 通过 `importlib.import_module()` + `getattr()` 动态解析。这与模型（`models[].use`）、沙箱（`sandbox.use`）、Guardrails Provider（`guardrails.provider.use`）使用同一套机制，保持配置风格一致。

**错误发现时机**：`get_available_tools()` 在 `make_lead_agent()` 调用时执行，即每次图构建时验证。配置错误会在第一个请求时暴露，而非需要单独的验证步骤。

---

### 决策 3：工具组（tool groups）概念

**问题**：不同角色的 Agent 应该看到不同的工具集（如只读 Agent 不应看到 `write_file`）。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 工具组过滤（当前） | 在配置层控制，不需要修改代码 | 需要在 config.yaml 中维护 group 映射 |
| 按 Agent 硬编码工具列表 | 简单 | 每添加工具需修改所有 Agent 配置 |
| 技能的 allowed-tools | 粒度更细 | 只在技能系统内生效 |

**选择工具组**：`config.yaml` 中的 `tools` 字段每个条目有 `group` 属性（如 `bash`、`file`、`web`）。自定义 Agent 的 `config.yaml` 可以指定 `tool_groups: ["file", "web"]`，`get_available_tools(groups=...)` 在加载时只包含匹配组的工具。这允许创建受限的 Agent 而不修改工具代码。

**host-bash 安全过滤**：当 `LocalSandboxProvider` 活跃时，`_is_host_bash_tool()` 检测 `group == "bash"` 或 `use == "deerflow.sandbox.tools:bash_tool"` 的工具并自动排除，防止绕过沙箱安全限制。

---

### 决策 4：view_image 条件绑定 supports_vision

**问题**：非视觉模型收到 `view_image` 工具后调用它，返回 base64 数据却无法处理。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 条件绑定（当前） | 非视觉模型不看到该工具，不会误用 | 需要知道模型是否支持视觉 |
| 始终绑定 + 错误提示 | 简单 | LLM 浪费 token 调用无用工具 |

**选择条件绑定**：`get_available_tools()` 检查 `model_config.supports_vision`，只有为 True 时才将 `view_image_tool` 添加到 `builtin_tools`。模型配置在 `config.yaml` 的 `models` 数组中声明。

**双重安全**：`_build_middlewares()` 中 `ViewImageMiddleware` 也做了同样的条件检查——只有 `supports_vision` 的模型才有中间件处理图像注入。工具和中间件的绑定保持一致。

---

### 决策 5：task 工具防止递归子代理调用

**问题**：子代理调用 `task` 工具创建子子代理，子子代理又调用 `task`...无限递归。

| 方案 | 优势 | 劣势 |
|------|------|------|
| `subagent_enabled=False`（当前） | 子代理的工具列表不含 task，无法嵌套 | 子代理无法进一步委派 |
| 递归深度限制 | 允许有限嵌套 | 复杂度指数增长；token 消耗难以控制 |

**选择硬性禁止**：`task_tool` 内部调用 `get_available_tools(subagent_enabled=False)` 构建子代理的工具集。子代理看到的工具列表中不包含 `task`，从根本上杜绝递归嵌套。

**技能白名单继承**：`_merge_skill_allowlists()` 合并父代理和子代理的技能白名单——取交集，确保子代理只能在父代理允许的范围内选择技能。如果父代理限制为 `["web_research"]`，子代理无法使用 `["system_admin"]` 技能。

**工具组继承**：子代理继承父代理的 `tool_groups`，遵守相同的工具访问限制。

---

## 三、实现效果

| 效果 | 实现方式 |
|------|----------|
| **零配置内置工具** | `present_files` + `ask_clarification` 始终可用 |
| **配置驱动外部工具** | `resolve_variable` 从 YAML 路径加载任意 Tool 实例 |
| **按角色工具隔离** | `tool_groups` 过滤 + 技能 `allowed-tools` 双重控制 |
| **视觉条件适配** | `supports_vision` 控制工具和中间件的绑定 |
| **防递归嵌套** | `subagent_enabled=False` 在子代理工具链中移除 task |
| **同步/异步双路径** | `make_sync_tool_wrapper` 自动为纯异步工具生成同步包装 |
| **去重安全** | 按 tool name 去重，config 工具优先级最高 |
