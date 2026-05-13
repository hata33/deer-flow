# 004-工具系统模块

> 已验证来源：deer-flow 项目 `tools/tools.py` + `tools/builtins/*` + `config/tool_config.py` + `sandbox/tools.py` + `sandbox/security.py`
> 本提示词可在新项目中直接使用，通过适配层注入新项目的工具来源和过滤条件，不需要修改本提示词本体。

---

## 一、设计意图

**为什么需要这个模块？**

AI Agent 的能力来自工具调用，但工具来源多样——配置文件定义的（bash、搜索）、内置的（文件展示、澄清）、MCP 服务器提供的、ACP 代理注入的。
不同工具的加载条件也不同：有的按分组过滤，有的按模型能力（vision）决定，有的按运行时参数（subagent_enabled）注入。
需要一个统一的组装层把所有来源按条件收集、过滤、返回给 Agent 工厂。

**解决的核心痛点：**
- 工具来源分散 → 四层来源叠加，统一入口
- 条件注入逻辑散落各处 → 集中在 `get_available_tools` 一个函数
- MCP 工具过多消耗 token → 延迟加载（tool_search）按需发现
- 本地沙箱不安全 → bash 工具过滤 + 路径校验 + 输出脱敏
- 子代理递归嵌套 → `subagent_enabled=False` 防递归

---

## 二、输入契约

| 输入项 | 来源 | 说明 |
|--------|------|------|
| `groups` | Agent 工厂 | 工具分组过滤列表 |
| `model_name` | Agent 工厂 | 判断 vision 能力 |
| `subagent_enabled` | Agent 工厂 | 是否注入子代理工具 |
| `include_mcp` | Agent 工厂 | 是否包含 MCP 工具 |
| `config.yaml` 中的 `tools` | 配置文件 | 配置驱动的工具定义 |

### 四层来源叠加顺序

```
第一层：配置工具（config.yaml → groups 过滤 → 反射加载）
第二层：内置工具（BUILTIN_TOOLS + 条件注入 task/view_image）
第三层：MCP 缓存工具（或延迟注册到 tool_search）
第四层：ACP 代理工具（动态构建）
```

---

## 三、输出契约

### 对外暴露的接口

```python
def get_available_tools(groups=None, include_mcp=True, model_name=None, subagent_enabled=False) -> list[BaseTool]:
    """返回 Agent 可用的完整工具列表。

    保证：
    - 配置工具已按 groups 过滤
    - 内置工具已按条件注入
    - 本地沙箱不安全工具已移除
    - MCP/ACP 工具已按需加载
    """
```

### 保证

| 保证项 | 说明 |
|--------|------|
| 配置工具已按 groups 过滤 | 不属于指定 group 的工具不出现在列表中 |
| 内置工具条件正确 | task 仅 subagent_enabled 时出现，view_image 仅 vision 模型时出现 |
| 本地沙箱安全 | bash 工具在本地沙箱 + 未允许时被移除 |
| 延迟工具每请求隔离 | ContextVar 保证并发请求互不干扰 |

---

## 四、行为约束

### 约束 1：groups 过滤只作用于配置工具

内置工具、MCP 工具、ACP 工具不受 groups 过滤。内置工具是基础能力，MCP/ACP 有独立启用机制。

### 约束 2：task_tool 防递归

子代理加载工具时传 `subagent_enabled=False`。否则子代理可以无限嵌套调用 task。

### 约束 3：反射加载，新增工具只改配置

```yaml
# config.yaml 新增工具，不改代码
- name: my_tool
  group: search
  use: mypackage.tools:my_tool
```

### 约束 4：延迟注册表每请求隔离

```python
_registry_var: contextvars.ContextVar[DeferredToolRegistry | None] = ...
```
模块级全局变量会在并发请求间共享状态，ContextVar 跟随异步上下文。

### 约束 5：本地沙箱三步安全流水线

```
校验路径合法（validate_local_tool_path）
    → 替换虚拟路径（replace_virtual_paths_in_command）
    → 执行命令
    → 输出脱敏（mask_local_paths_in_output）
```
任何一步缺失都有安全风险。

### 约束 6：tool_search promote 后不过滤

`registry.promote({t.name for t in matched_tools})` 将匹配工具从延迟列表移除。
后续 `DeferredToolFilterMiddleware` 不再拦截这些工具的 bind_tools 调用。

---

## 五、验证场景

| # | Given | When | Then |
|---|-------|------|------|
| 1 | groups=["bash"] | get_available_tools | 配置工具只含 bash 组 + 全部内置 |
| 2 | groups=None | get_available_tools | 全部配置工具 + 全部内置 |
| 3 | subagent_enabled=false | get_available_tools | 不含 task_tool |
| 4 | subagent_enabled=true | get_available_tools | 含 task_tool |
| 5 | model 不支持 vision | get_available_tools | 不含 view_image_tool |
| 6 | 本地沙箱 + 未允许 bash | get_available_tools | bash 工具被过滤 |
| 7 | tool_search 启用 | get_available_tools | MCP 延迟注册 + tool_search 注入 |
| 8 | bash 命令含 `..` | bash_tool | PermissionError |
| 9 | 子代理工具列表 | task_tool 内部调用 | subagent_enabled=False，无 task |

---

## 六、自由度与禁区

### 可以改的

- 内置工具列表（按项目需求增减）
- 工具来源（不使用 MCP/ACP 则不加载）
- 沙箱工具集（只用 bash + read_file，不需要全部五个）
- 延迟加载策略（不启用 tool_search）
- 路径安全校验规则（不同沙箱方案有不同威胁模型）

### 不能改的

- **四层来源叠加**：混在一起无法按需启停
- **groups 只过滤配置工具**：内置工具是基础能力
- **task_tool 防递归**：`subagent_enabled=False`
- **ContextVar 隔离**：全局变量导致并发污染
- **本地沙箱三步流水线**：缺一步有安全风险
- **反射加载**：新增工具只改配置

---

## 七、依赖的上下游模块

```
[上游] 配置系统 → get_app_config(), config.tools
[上游] 反射系统 → resolve_variable(path, BaseTool)
[上游] 沙箱系统 → ensure_sandbox_initialized(), 路径校验
[上游] MCP 系统 → get_cached_mcp_tools()
[上游] ACP 系统 → get_acp_agents()
    ↓
[本模块] 工具系统
    ↓
[下游] Agent 工厂 → get_available_tools(groups, model_name, subagent_enabled)
```
