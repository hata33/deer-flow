# 001-Agent工厂构建

## 解决什么问题

每次请求需要动态组装一个 Agent：模型、工具集、中间件链、提示词、状态模式。
这些部件的创建策略各不相同——有的读配置，有的依赖请求参数，有的需要条件判断后降级。
工厂把它们集中到一个地方，调用方只传 config。

## 为什么两层

- SDK 层（`create_deerflow_agent`）：纯参数组装，不读配置文件，不依赖全局单例。子智能体和外部集成直接调用。
- 应用层（`make_lead_agent`）：读配置、处理降级策略、注入追踪。业务策略变化只改这一层。
- 两层解耦后 SDK 层可独立测试，不需要模拟配置文件。
- **应用层自己组装中间件（`_build_middlewares`），不调 SDK 层。**

## 本模块的职责边界

**只负责组装策略**：决定用哪个模型、加载哪些工具、排列哪些中间件、填什么提示词。
不负责：模型实例化（模型工厂模块）、工具实现（工具模块）、中间件实现（中间件模块）、配置加载（配置模块）。

## 不可变的设计决策

以下决策来自实战踩坑，新项目中不可省略或改变：

**三级优先级链**：请求参数 → agent_config.model → 配置默认模型。每一级对应一个使用场景（子智能体不传 model_name、新建对话无 agent_config），去掉任何一级都会在某条路径上报错。

**能力验证先于实例化，但降级不抛异常**：thinking=true 但模型不支持 → 降级 false + warning。用户传了 thinking=true 只是期望，不是硬性要求。

**互斥验证用 `is not None` 而非 truthy**：空列表 `[]` 是合法的"完全接管模式（接管后无中间件）"，`if middleware and features` 会把 `[]` 错误放过。

**三态特性标志**：True/False/实例。没有合理默认值的特性，True 必须 raise 而非静默跳过。

**终端中间件不变量**：无论怎么插入，ClarificationMiddleware 必须在链尾。@Next 可能把它推离末位，插入后必须强制归位。

**工具去重用户优先**：特性注入的工具（如 view_image_tool）和用户提供的工具按 name 去重，用户版本胜出。

**声明式定位而非索引**：@Next/@Prev 让调用方说"在谁旁边"而非"在位置几"，框架内部链顺序变化时不会静默错位。

**延迟导入避免循环依赖**：真实代码在函数体内 `from deerflow.tools import get_available_tools`，而非文件顶部 import。

## 适配层

```yaml
<ADAPT>
# === 框架 ===
framework: "langgraph"
create_agent_fn: "create_agent"
config_type: "RunnableConfig"

# === 基类 ===
model_type: "BaseChatModel"
tool_type: "BaseTool"
middleware_type: "AgentMiddleware"
state_schema: "ThreadState"              # 必须含 messages: list

# === 外部依赖函数（填入你项目中的实际签名）===
create_chat_model: "create_chat_model(name, thinking_enabled, reasoning_effort) -> BaseChatModel"
get_available_tools: "get_available_tools(model_name, groups, subagent_enabled) -> list[BaseTool]"
apply_prompt_template: "apply_prompt_template(subagent_enabled, max_concurrent, agent_name) -> str"
load_agent_config: "load_agent_config(agent_name) -> AgentConfig | None"
get_model_config: "get_model_config(name) -> ModelConfig | None"
get_app_config: "get_app_config() -> AppConfig"
</ADAPT>
```

## 自检清单

| # | 验证 | 期望 |
|---|------|------|
| 1 | 不指定 model_name | 降级到默认模型 |
| 2 | 指定不存在的 model_name | warning + 回退默认，不报错 |
| 3 | models 为空 | ValueError |
| 4 | thinking=true + 不支持 | 降级 false + warning，不报错 |
| 5 | thinking=false + 有配置 | 显式传 disabled，非省略 |
| 6 | 两次调用不同 config | 完全独立的实例 |
| 7 | middleware + features 同时传 | ValueError |
| 8 | 特性 True 但无内置默认 | ValueError（非静默跳过） |
| 9 | 用户工具与特性注入工具同名 | 用户版本胜出 |
| 10 | @Next 把终端中间件推离末位 | 强制归位 |
| 11 | 两个 @Next 同锚点 | ValueError 冲突 |
| 12 | @Next(A)+@Next(B) 循环 | ValueError |

## 依赖模块

| 模块 | 本模块调用的接口 |
|------|----------------|
| **配置系统** | `get_app_config()` / `get_model_config()` / `load_agent_config()` |
| **模型工厂** | `create_chat_model(name, thinking_enabled, reasoning_effort)` |
| **工具系统** | `get_available_tools(model_name, groups, subagent_enabled)` |
| **中间件系统** | 各具体中间件类的构造函数 |
| **提示词模板** | `apply_prompt_template(subagent_enabled, max_concurrent, agent_name)` |
| **状态模式** | `ThreadState` 类定义 |

---

## 参考源码

以下是已验证的源码实现。阅读并理解其模式，然后根据 `<ADAPT>` 适配参数在新项目中重写。
不要复制粘贴——重点关注每个函数的**决策分支**和**边界条件处理**。

### 文件清单（按阅读顺序）

| 文件 | 职责 | 阅读重点 |
|------|------|---------|
| `agents/features.py` | 特性标志 + @Next/@Prev 装饰器 | 三态字段的类型声明方式；装饰器如何在类上设置 `_next_anchor` |
| `agents/factory.py` | SDK 层工厂 | `create_deerflow_agent` 的互斥验证；`_assemble_from_features` 中三态模板的 7 次重复应用；`_insert_extra` 的迭代解析算法 |
| `agents/lead_agent/agent.py` | 应用层工厂 | `make_lead_agent` 的三级优先级解析；`_build_middlewares` 的条件判断来源；`_resolve_default` 的回退判断方式 |

源码文件见同目录下的 `src/` 子文件夹。
