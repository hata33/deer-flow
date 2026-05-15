deer-flow-main\backend\packages\harness\deerflow\runtime\runs\worker.py:87

承接 005 第 3 步 `agent = agent_factory(config=runnable_config)`，这一行调用 `make_lead_agent`，触发一整套项目模块的初始化链路。到第 5 步 `agent.astream()` 时，agent 已经是完全就绪的状态

---

**004 `start_run` 中**（参数准备，不触发实际初始化）：

``` python
agent_factory = resolve_agent_factory(body.assistant_id)  # 返回 make_lead_agent 函数引用
graph_input = normalize_input(body.input)                  # 转换输入格式
config = build_run_config(thread_id, body.config, body.metadata)  # 组装 RunnableConfig
stream_modes = normalize_stream_modes(body.stream_mode)    # 规范化流式模式
```

这一步只是拿到函数引用和准备参数，`make_lead_agent` 还没有被调用

---

**005 `run_agent` 第 3 步中**（Runtime 注入 + agent 创建）：

``` python
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

# 注入运行时上下文（langgraph-cli 自动注入，嵌入式需手动）
runtime = Runtime(context={"thread_id": thread_id}, store=store)
config.setdefault("configurable", {})["__pregel_runtime"] = runtime

runnable_config = RunnableConfig(**config)
agent = agent_factory(config=runnable_config)
```

这段代码做了三件事：

**创建 LangGraph Runtime**：`Runtime(context={"thread_id": thread_id}, store=store)` 创建运行时上下文对象，携带当前线程 ID 和 store 引用。在 LangGraph CLI 部署模式下这些会自动注入，但这里是嵌入式执行（Gateway 直接调用），需要手动构建

**注入到 config**：将 runtime 实例塞入 `config["configurable"]["__pregel_runtime"]`。LangGraph 图内部通过这个键获取 Runtime，从而访问 store 和 thread 上下文。`__pregel_` 前缀是 LangGraph 内部约定的命名空间

**转换为 RunnableConfig**：将普通 dict 转为 LangChain 的 `RunnableConfig` 类型，传给 `agent_factory`（即 `make_lead_agent`）。`make_lead_agent` 从 `config.configurable` 中提取各种运行时参数（模型名、thinking 开关等），runtime 对象也会随 config 一起传入图内部

经过这一步，config 中同时携带了两类信息：
- **业务参数**（004 准备的）：`thread_id`、`model_name`、`is_plan_mode`、`subagent_enabled` 等
- **运行时基础设施**（本步注入的）：`__pregel_runtime`（store + thread context）

之后调用 `agent_factory(config=runnable_config)` 触发完整初始化链路：

**加载配置**

- `get_app_config()` → 读取全局 `config.yaml`，解析模型列表、工具配置、沙箱配置等
- `load_agent_config(agent_name)` → 读取自定义智能体目录下的 `agents_config.yaml`，获取模型名和工具组

**解析模型**

- `_resolve_model_name()` → 三级优先级：请求参数 > 智能体配置 > 全局默认，校验模型名是否存在
- `app_config.get_model_config(model_name)` → 获取模型详细配置（provider、thinking 支持、vision 支持等）
- 验证模型能力：模型不支持 thinking 时自动降级

**创建 LLM 实例**

- `create_chat_model(name=model_name, thinking_enabled=..., reasoning_effort=...)` → 模型工厂函数
  - 根据 config.yaml 中的 `use` 字段反射导入 LangChain 的 model 类（如 `ChatOpenAI`、`ChatAnthropic`）
  - 注入 thinking 配置、reasoning_effort 等 provider 特定参数
  - 配置值中 `$` 开头的解析为环境变量

**组装工具集**

- `get_available_tools(model_name, groups, subagent_enabled)` → 从多个来源组装工具列表：
  1. config.yaml 中定义的工具 → `resolve_variable()` 反射加载
  2. MCP 工具 → `get_cached_mcp_tools()` 懒加载 + mtime 缓存失效
  3. 内置工具 → `present_files`、`ask_clarification`、`view_image`（vision 模型才有）
  4. 社区工具 → `tavily`、`jina_ai`、`firecrawl`
  5. 子智能体工具 → `task`（`subagent_enabled=True` 时）
  6. ACP 工具 → 外部 ACP 智能体调用

**构建中间件链**

- `_build_middlewares(config, model_name, agent_name)` → 按严格顺序组装中间件
  - `build_lead_runtime_middlewares()` → 基础层（ThreadData、Sandbox、Guardrail、ToolError 等）
  - 条件层根据配置动态追加（Summarization、Todo、Title、Memory、ViewImage、SubagentLimit、Clarification 等）

**生成系统提示词**

- `apply_prompt_template(subagent_enabled, agent_name)` → 动态组装系统提示词
  - 注入记忆上下文（`<memory>` 标签）
  - 注入 skills 列表（XML 格式的技能描述和文件路径）
  - 注入子智能体指令（并发限制、任务编排规则）
  - 注入 ACP 指令（工作区路径、只读访问）
  - 加载 SOUL.md 智能体人格文件

**创建编译图**

- `create_agent(model, tools, middleware, system_prompt, state_schema=ThreadState)` → LangChain 的 API
  - 内部创建 ReAct 循环图（LLM → 工具 → LLM → ...）
  - 绑定中间件到图的各执行阶段
  - 返回 `CompiledStateGraph`，即 `agent.astream()` 调用的那个对象

---

**初始化完成后回到 005**，agent 挂载上 checkpointer、store、中断节点配置，进入第 5 步 `agent.astream()` 开始流式执行

> 本步骤：先构建 LangGraph Runtime（thread context + store）注入到 config，再将携带业务参数和运行时基础设施的 RunnableConfig 传给 `agent_factory`，触发完整初始化链路——加载配置、解析模型、创建 LLM、组装工具集、构建中间件链、生成系统提示词，最终通过 `create_agent` 产出编译好的 LangGraph 状态图
