# Tools 工具系统——底层逻辑与本质

## 一句话本质

工具不是硬编码的 import，而是 **config.yaml 中声明的字符串路径**，运行时通过反射动态解析。新增工具 = 安装包 + 加一行配置，零代码修改。

---

## 1. 字符串路径解析——插件架构的核心

```yaml
# config.yaml
tools:
  - name: bash
    group: sandbox
    use: deerflow.sandbox.tools:bash_tool      # 字符串路径
  - name: web_search
    group: search
    use: deerflow.community.tavily.tools:web_search_tool
```

`use` 字段是 `"module.path:variable_name"` 格式的字符串。`resolve_variable()` 在运行时通过 `importlib.import_module()` 动态加载：

```python
def resolve_variable(path: str):
    module_path, attr_name = path.rsplit(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr_name)
```

**为什么不用直接 import？** 直接 import 意味着工具和 Agent 代码紧耦合。字符串路径 + 反射实现了真正的插件架构：
- 用户安装一个新 Python 包（`uv add langchain-google-genai`）
- 在 config.yaml 加一行 `use: langchain_google_genai:some_tool`
- 重启即生效，Agent 代码完全不动

**核心启示**：把"用什么"和"怎么用"分开。配置声明"用什么"（字符串路径），代码负责"怎么用"（反射加载 + 类型校验）。这是 OSGi/SPI 思想在 Agent 领域的直接应用。

## 2. 工具组装管线——五阶段流水线

`get_available_tools()` 不是简单返回一个列表，而是经过五阶段管线：

```
阶段 1: Config-defined tools（config.yaml 声明的工具）
    │  ↓ 按 groups 过滤、按安全策略裁剪、resolve_variable 解析
阶段 2: Built-in tools（按条件添加）
    │  ↓ present_file（始终）、ask_clarification（始终）
    │  ↓ task（仅 subagent_enabled=True）
    │  ↓ view_image（仅 supports_vision=True）
    │  ↓ tool_search（仅 MCP 存在 + tool_search.enabled）
阶段 3: MCP tools（延迟加载 + 缓存）
    │  ↓ 从 extensions_config.json 读取服务器配置
    │  ↓ 首次调用时连接 MCP 服务器获取工具列表
    │  ↓ mtime 变更检测做缓存失效
阶段 4: ACP agent tools（外部智能体调用工具）
    │  ↓ 从 config.yaml 读取 ACP 智能体配置
阶段 5: Deduplication（去重，优先级: config > builtin > MCP > ACP）
```

**核心启示**：工具来源是多样的（配置文件、内置代码、MCP 协议、外部智能体），但调用方不需要关心来源。统一的五阶段管线把它们聚合成一个 `list[BaseTool]`，Agent 只看到统一的工具接口。新增一种工具来源只需在管线中加一个阶段。

## 3. Group 过滤——同一套工具定义，不同上下文不同子集

```yaml
# config.yaml
tool_groups:
  - name: default    # 主 Agent 用
  - name: research   # 研究类 Agent 用
  - name: minimal    # 轻量 Agent 用

tools:
  - name: bash
    group: sandbox       # 属于 sandbox 组
  - name: web_search
    group: search        # 属于 search 组
  - name: read_file
    group: sandbox
```

```python
# 主 Agent：获取全部工具
tools = get_available_tools(groups=None)

# 自定义 Agent：只获取指定组的工具
tools = get_available_tools(groups=["sandbox", "search"])

# 子 Agent：继承父 Agent 的工具，但 subagent_enabled=False（禁用递归）
tools = get_available_tools(subagent_enabled=False)
```

**核心启示**：工具不是"全给"或"全不给"的二选一。Group 机制让同一套工具定义为不同角色服务——主 Agent 拥有全部能力，子 Agent 只继承安全子集，研究 Agent 只获得搜索工具。这和 Linux 的 capability-based security 是同一思路：按角色分配最小权限。

## 4. 条件性工具——运行时能力决定工具可见性

工具不是静态的列表，而是根据运行时上下文动态裁剪：

| 条件 | 工具 | 原因 |
|------|------|------|
| `subagent_enabled=True` | `task` | 防止未启用子智能体时误调用 |
| `supports_vision=True` | `view_image` | 模型不支持图片时调用无意义 |
| MCP 服务器启用 | MCP 工具集 | 未配置时不存在这些工具 |
| `tool_search.enabled` | `tool_search` | 延迟工具发现模式 |
| `is_host_bash_allowed()` | `bash` 系列 | 本地沙箱未授权时禁用命令执行 |

**核心启示**：工具集是运行时的"能力快照"。不要给 Agent 它用不了的工具——LLM 看到工具定义就会尝试调用，调用失败（模型不支持、环境不允许）只会浪费 token 和轮次。在工具注册阶段就按能力裁剪，比在提示词中告诉"别用这个工具"可靠得多。

## 5. 信号工具模式——工具函数只是触发器，中间件才是执行者

`ask_clarification` 工具的函数体只有一行 `return "Clarification requested"`。真正的行为由 `ClarificationMiddleware` 实现：拦截工具调用 → 格式化问题 → `Command(goto=END)` 中断执行。

```python
# 工具定义（纯信号）
@tool(return_direct=True)
def ask_clarification(question, clarification_type, ...):
    return "Clarification requested"

# 中间件拦截（真正行为）
class ClarificationMiddleware:
    def wrap_tool_call(self, ...):
        if tool_name == "ask_clarification":
            return Command(
                update={"messages": [formatted_question]},
                goto=END  # 中断执行
            )
```

**核心启示**：当工具的目的不是"执行操作"而是"改变控制流"时，把工具定义和执行分离。工具是 LLM 可以调用的"信号"，中间件是响应信号的"处理器"。这让控制流变更（中断、跳转、暂停）对 LLM 透明——LLM 只知道"我调用了 ask_clarification"，不知道这个调用会中断整个执行。

## 6. Command 模式——工具通过状态更新与 Agent 通信

`present_file` 和 `view_image` 不返回字符串，而是返回 LangGraph 的 `Command` 对象：

```python
# present_file 返回 Command 更新状态
return Command(
    update={
        "artifacts": [file_path],         ← 追加到 artifacts 列表
        "messages": [ToolMessage(...)],    ← 告诉 LLM 文件已展示
    }
)
```

`ThreadState` 上的 `artifacts` 字段有自定义 reducer `merge_artifacts`（去重合并），`viewed_images` 有 `merge_viewed_images`（字典覆盖 + 清空语义）。工具不需要知道状态如何合并，只需要返回想要更新的值。

**核心启示**：工具不应只返回字符串给 LLM。复杂 Agent 的工具需要同时做两件事：(1) 告诉 LLM 结果 (2) 更新 Agent 状态。`Command` 模式让工具成为状态的写入者，reducer 处理并发合并。这比让工具直接修改全局变量安全得多——状态更新是声明式的，不是命令式的。
