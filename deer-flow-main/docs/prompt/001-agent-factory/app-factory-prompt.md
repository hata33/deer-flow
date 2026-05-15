# 001b-应用层工厂（agent.py）

## 解决什么问题

`make_lead_agent` 是面向业务的工厂入口——从请求 config 和配置文件中提取参数，处理模型降级策略，注入追踪元数据，最终组装完整的 Lead Agent。

与 SDK 层 `create_deerflow_agent` 的分工：
- **SDK 层（factory.py）**：纯参数组装，无 I/O，无配置。可独立测试。
- **应用层（本模块）**：读配置 + 处理降级 + 注入追踪。业务策略变化只改这一层。
- **当前实现**：应用层自己组装中间件（`_build_middlewares`），不走 SDK 层的 `features` 路径。两套组装路径独立存在。

## 职责边界

**只负责组装策略**：决定用哪个模型、加载哪些工具、排列哪些中间件、填什么提示词。
不负责：模型实例化（模型工厂模块）、工具实现（工具模块）、中间件实现（中间件模块）、配置加载（配置模块）。

## 不可变的设计决策

### 三级优先级链

模型名称解析按三级降级：

```
请求参数 config.configurable["model_name"]     ← 最高
    ↓ (None 时降级)
Agent 配置 agent_config.model                  ← 中等
    ↓ (None 时降级)
全局配置 config.yaml models[0].name             ← 默认兜底
```

每一级对应一个使用场景：子智能体不传 model_name、新建对话无 agent_config、config.yaml 必须有兜底。去掉任何一级都会在某条路径上报错。

### 能力验证先于实例化，但降级不抛异常

```python
# thinking=true 但模型不支持 → 降级 false + warning
if thinking_enabled and not model_config.supports_thinking:
    logger.warning(...)
    thinking_enabled = False
```

用户传了 `thinking=true` 只是期望，不是硬性要求。降级而非报错，保证请求不被中断。

### 应用层自己组装中间件

`_build_middlewares` 手动排列中间件，不调 SDK 层的 `create_deerflow_agent(features=...)`。

**Why**: 应用层需要根据运行时参数（is_plan_mode、subagent_enabled、supports_vision 等）和配置文件动态决定中间件组合，features 声明式模型不如直接 if/else 灵活。

### 每次调用创建全新实例

工厂函数必须无状态。`asyncio.create_task(run_agent)` 每次触发 `agent_factory(config)` 构建新 Agent。

**Why**: 运行时参数（模型、工具、中间件）可能每次请求都不同，缓存会导致跨请求状态泄漏。

## make_lead_agent 执行流程

```
make_lead_agent(config: RunnableConfig)
    │
    ├── 1. 从 config.configurable 提取 9 个参数
    │
    ├── 2. 三级优先级解析模型名称
    │       → _resolve_model_name()
    │
    ├── 3. 验证模型能力（thinking 降级）
    │
    ├── 4. 注入 LangSmith 追踪元数据
    │
    └── 5. create_agent(
            model       = create_chat_model(name, thinking_enabled, reasoning_effort),
            tools       = get_available_tools(model_name, groups, subagent_enabled),
            middleware  = _build_middlewares(config, model_name),
            system_prompt = apply_prompt_template(subagent_enabled, ...),
            state_schema = ThreadState,
        ) → CompiledStateGraph
```

### 5 个子系统的组装

| 子系统 | 工厂函数 | 输出 |
|--------|---------|------|
| 模型 | `create_chat_model(name, thinking_enabled, reasoning_effort)` | `BaseChatModel` 实例 |
| 工具 | `get_available_tools(model_name, groups, subagent_enabled)` | `list[BaseTool]` |
| 中间件 | `_build_middlewares(config, model_name, agent_name)` | `list[AgentMiddleware]` |
| 提示词 | `apply_prompt_template(subagent_enabled, max_concurrent, agent_name)` | `str` |
| 状态 | `ThreadState` 类 | LangGraph 状态模式 |

## _build_middlewares 中间件组装

手动按严格顺序组装，后续中间件依赖前置中间件设置的状态：

```
build_lead_runtime_middlewares()           ← 基础运行时（7 个）
    ThreadDataMiddleware                   — before_agent: 创建线程目录
    UploadsMiddleware                      — before_agent: 注入上传文件
    SandboxMiddleware                      — before_agent: 懒初始化沙箱
    DanglingToolCallMiddleware             — wrap_model_call: 修补缺失 ToolMessage
    GuardrailMiddleware                    — wrap_tool_call: 工具调用授权
    SandboxAuditMiddleware                 — wrap_tool_call: bash 命令审计
    ToolErrorHandlingMiddleware            — wrap_tool_call: 工具异常兜底

条件中间件（按运行时参数决定是否添加）：
    SummarizationMiddleware    (条件: config.yaml enabled)
    TodoMiddleware             (条件: is_plan_mode)
    TokenUsageMiddleware       (条件: config.yaml token_usage.enabled)
    TitleMiddleware             (始终)
    MemoryMiddleware            (始终)
    ViewImageMiddleware         (条件: model supports_vision)
    DeferredToolFilterMiddleware(条件: config.yaml tool_search.enabled)
    SubagentLimitMiddleware     (条件: subagent_enabled)
    LoopDetectionMiddleware     (始终)

终端保证：
    ClarificationMiddleware    (始终，最后) — 拦截澄清请求，Command(goto=END)
```

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
state_schema: "ThreadState"

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

| # | 验证 | 期望 | 代码位置 |
|---|------|------|---------|
| 1 | 不指定 model_name | 降级到默认模型 | agent.py:_resolve_model_name |
| 2 | 指定不存在的 model_name | warning + 回退默认，不报错 | agent.py:_resolve_model_name |
| 3 | models 为空 | ValueError | agent.py:_resolve_model_name |
| 4 | thinking=true + 模型不支持 | 降级 false + warning，不报错 | agent.py:332-334 |
| 5 | thinking=false + 有配置 | 显式传 disabled，非省略 | models/factory.py |
| 6 | 两次调用不同 config | 完全独立的实例 | agent.py:274 每次新建 |
| 7 | agent_name 指向自定义智能体 | 加载对应 tool_groups 和 system_prompt | agent.py:319, 376-378 |

## 依赖模块

| 模块 | 本模块调用的接口 |
|------|----------------|
| **配置系统** | `get_app_config()` / `get_model_config()` / `load_agent_config()` |
| **模型工厂** | `create_chat_model(name, thinking_enabled, reasoning_effort)` |
| **工具系统** | `get_available_tools(model_name, groups, subagent_enabled)` |
| **中间件系统** | 各具体中间件类的构造函数 + `build_lead_runtime_middlewares()` |
| **提示词模板** | `apply_prompt_template(subagent_enabled, max_concurrent, agent_name)` |
| **状态模式** | `ThreadState` 类定义 |

---

## 参考源码

以下是已验证的源码实现。阅读并理解其模式，然后根据 `<ADAPT>` 适配参数在新项目中重写。
不要复制粘贴——重点关注每个函数的**决策分支**和**边界条件处理**。

### 文件清单（按阅读顺序）

| 文件 | 职责 | 阅读重点 |
|------|------|---------|
| `agents/lead_agent/agent.py` | 应用层工厂 | `make_lead_agent` 的三级优先级解析；`_build_middlewares` 的条件判断来源；`_resolve_model_name` 的回退判断方式 |
| `agents/lead_agent/prompt.py` | 提示词模板 | `apply_prompt_template` 的各段拼接逻辑 |
| `agents/thread_state.py` | 状态模式 | `ThreadState` 的字段定义和自定义 reducer |

源码文件见同目录下的 `src/` 子文件夹。
