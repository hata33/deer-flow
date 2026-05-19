deer-flow-main\backend\packages\harness\deerflow\agents\lead_agent\agent.py

承接 006，`make_lead_agent(config)` 是 agent 工厂函数，根据运行时配置创建完整的 LangGraph 编译图。`create_agent(model, tools, middleware, system_prompt, state_schema)` 是 LangChain 提供的 agent 创建 API，`make_lead_agent` 负责准备好所有传参

---

**第一步：提取运行时参数**

``` python
cfg = config.get("configurable", {})
thinking_enabled = cfg.get("thinking_enabled", True)
reasoning_effort = cfg.get("reasoning_effort", None)
requested_model_name = cfg.get("model_name") or cfg.get("model")
is_plan_mode = cfg.get("is_plan_mode", False)
subagent_enabled = cfg.get("subagent_enabled", False)
max_concurrent_subagents = cfg.get("max_concurrent_subagents", 3)
is_bootstrap = cfg.get("is_bootstrap", False)
agent_name = cfg.get("agent_name")
```

从 `config.configurable` 中提取前端或运行时传入的可选参数，控制 agent 的行为模式

---

**第二步：解析模型名称（三级优先级）**

``` python
agent_config = load_agent_config(agent_name) if not is_bootstrap else None
agent_model_name = agent_config.model if agent_config and agent_config.model else _resolve_model_name()
model_name = requested_model_name or agent_model_name
```

优先级：请求参数中的 `model_name` > 智能体配置（`agents_config.yaml`）中的 `model` > 全局默认模型（`config.yaml` 中第一个模型）

`_resolve_model_name()` 会校验模型名是否在配置中存在，无效则回退到默认并打印警告。没有配置任何模型时直接抛异常

---

**第三步：验证模型配置**

``` python
model_config = app_config.get_model_config(model_name)
if thinking_enabled and not model_config.supports_thinking:
    thinking_enabled = False
```

检查模型是否支持请求的功能。比如模型不支持思考模式但 `thinking_enabled=True`，自动降级为关闭

---

**第四步：注入 LangSmith 追踪元数据**

``` python
config["metadata"].update({
    "agent_name": agent_name or "default",
    "model_name": model_name,
    "thinking_enabled": thinking_enabled,
    ...
})
```

将关键参数写入 config 的 metadata，用于 LangSmith 链路追踪

---

**第五步：创建智能体实例（核心）**

分两种模式：

**引导模式**（`is_bootstrap=True`）：

``` python
return create_agent(
    model=create_chat_model(name=model_name, thinking_enabled=thinking_enabled),
    tools=get_available_tools(model_name=model_name, subagent_enabled=subagent_enabled) + [setup_agent],
    middleware=_build_middlewares(config, model_name=model_name),
    system_prompt=apply_prompt_template(subagent_enabled=..., available_skills={"bootstrap"}),
    state_schema=ThreadState,
)
```

精简配置，额外加 `setup_agent` 工具，skills 只注入 `bootstrap`，用于初始自定义智能体创建流程

**默认模式**（标准主智能体）：

``` python
return create_agent(
    model=create_chat_model(name=model_name, thinking_enabled=thinking_enabled, reasoning_effort=reasoning_effort),
    tools=get_available_tools(model_name=model_name, groups=agent_config.tool_groups if agent_config else None, subagent_enabled=subagent_enabled),
    middleware=_build_middlewares(config, model_name=model_name, agent_name=agent_name),
    system_prompt=apply_prompt_template(subagent_enabled=..., agent_name=agent_name),
    state_schema=ThreadState,
)
```

两个模式传给 `create_agent` 的五个参数完全一致，只是各自的值不同：

- `model` — `create_chat_model` 根据 `model_name` 从配置反射创建 LLM 实例，`thinking_enabled` 控制是否开启思考，`reasoning_effort` 控制推理力度
- `tools` — `get_available_tools` 从多个来源组装工具集：config.yaml 定义的 → MCP 工具 → 内置工具（present_files、ask_clarification、view_image）→ 社区工具（tavily、jina_ai）→ 子智能体工具（task）→ ACP 工具。`groups` 参数可按自定义智能体配置筛选工具组
- `middleware` — `_build_middlewares` 构建中间件链（见下文）
- `system_prompt` — `apply_prompt_template` 动态组装系统提示词，注入记忆上下文、skills 列表、子智能体指令、ACP 指令等
- `state_schema` — `ThreadState`，扩展自 LangGraph 的 `AgentState`，增加了 `sandbox`、`thread_data`、`title`、`artifacts`、`todos`、`uploaded_files`、`viewed_images` 等线程级状态字段

---

**中间件链构建**（`_build_middlewares`）

中间件按严格顺序执行，分为两层：

**基础层**（`build_lead_runtime_middlewares` 提供，始终存在）：
1. `ThreadDataMiddleware` — 创建线程隔离目录
2. `UploadsMiddleware` — 追踪上传文件注入会话
3. `SandboxMiddleware` — 获取沙箱，存入 state
4. `DanglingToolCallMiddleware` — 修补缺失的 ToolMessage
5. `GuardrailMiddleware` — 工具调用授权（可选）
6. `SandboxAuditMiddleware` — 执行日志
7. `ToolErrorHandlingMiddleware` — 工具异常转 ToolMessage

**条件层**（根据配置动态添加）：
8. `SummarizationMiddleware` — 上下文过长时自动摘要（需配置启用）
9. `TodoMiddleware` — 计划模式下的任务追踪（`is_plan_mode=True`）
10. `TokenUsageMiddleware` — token 用量追踪（需配置启用）
11. `TitleMiddleware` — 首次对话后自动生成标题
12. `MemoryMiddleware` — 排队会话进行记忆更新
13. `ViewImageMiddleware` — 注入 base64 图片（模型支持 vision 时）
14. `DeferredToolFilterMiddleware` — 隐藏延迟加载工具的 schema（工具搜索启用时）
15. `SubagentLimitMiddleware` — 截断超额并发子智能体调用（`subagent_enabled=True`）
16. `LoopDetectionMiddleware` — 检测并打断重复工具调用循环
17. `ClarificationMiddleware` — 拦截澄清请求，始终在最后

条件层的中间件根据运行时 config 和模型能力决定是否添加，添加后追加到基础层后面

---

**`create_agent` 做了什么**

`create_agent` 是 LangChain 的 `langchain.agents.create_agent`，接收上面五个参数后：
- 创建一个 `CompiledStateGraph`（LangGraph 编译图）
- 图内部是一个 ReAct 循环：LLM 调用 → 工具执行 → LLM 调用 → ... 直到无工具调用或达到 recursion_limit
- 中间件在循环的各阶段插入（模型调用前/后、工具调用前/后）
- 最终返回的 `agent` 就是 005 中 `agent.astream()` 调用的那个对象

> 本步骤：`make_lead_agent` 作为工厂函数，根据运行时配置解析模型、组装工具集、构建中间件链、生成系统提示词，最终通过 LangChain 的 `create_agent` 创建编译好的 LangGraph 状态图，交给 005 的 `agent.astream()` 执行
