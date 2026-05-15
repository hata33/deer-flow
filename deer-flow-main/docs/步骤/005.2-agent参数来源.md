deer-flow-main\backend\packages\harness\deerflow\agents\lead_agent\agent.py

创建 agent 的参数由两个来源合并而成

---

**来源一：请求时传入的 body 参数（前端/运行时控制）**

来自 004 中 `start_run` 接收的 `RunCreateRequest`，经过 `build_run_config()` 写入 `config.configurable`：

``` python
config = build_run_config(thread_id, body.config, body.metadata)
```

传入 `config.configurable` 中的运行时参数：

- `model_name` / `model` — 本次使用哪个模型
- `thinking_enabled` — 是否开启思考模式
- `reasoning_effort` — 推理力度（low/medium/high）
- `is_plan_mode` — 是否启用计划模式（TodoList）
- `subagent_enabled` — 是否启用子智能体委托
- `max_concurrent_subagents` — 最大并发子智能体数
- `is_bootstrap` — 是否为初始化引导模式
- `agent_name` — 自定义智能体名称

每次调用可以不同，控制**本次** agent 的行为模式

---

**来源二：本地 config.yaml（静态配置）**

通过 `get_app_config()` 读取（详见 005.1），提供全局共享的基础设施配置：

- `models[]` — 模型列表，每个模型有 provider 类路径（`use`）、api_key、`supports_thinking`、`supports_vision`、`when_thinking_enabled` 等
- `tools[]` — 工具定义（`use` 变量路径 + `group`）
- `tool_groups[]` — 工具分组
- `sandbox` — 沙箱配置
- `skills` — 技能路径
- `token_usage` — token 用量追踪开关
- `tool_search` — 工具搜索开关
- `summarization` — 上下文摘要配置
- `memory` — 记忆系统配置
- `guardrails` — 工具调用防护配置

这些决定了"模型怎么连、工具有哪些、中间件配了什么"

---

**两者的合并方式**

在 `make_lead_agent(config)` 中，两路参数交汇：

``` python
cfg = config.get("configurable", {})    # 来源一：body 参数
app_config = get_app_config()           # 来源二：config.yaml
```

- body 参数的 `model_name` 通过 `_resolve_model_name()` 和 `app_config.get_model_config()` 从 config.yaml 的模型列表中找到对应配置，再传给 `create_chat_model()` 创建 LLM 实例
- body 参数的 `is_plan_mode`、`subagent_enabled` 等控制是否添加条件中间件
- config.yaml 的 `tools[]` 决定有哪些工具可用，`tool_groups` 按智能体配置筛选
- config.yaml 的 `sandbox`、`memory` 等决定基础中间件的行为

```
body 参数（每次不同）        config.yaml（全局共享）
       ↓                          ↓
 config.configurable          get_app_config()
       ↓                          ↓
       └──────── 合并 ─────────────┘
                   ↓
            make_lead_agent()
                   ↓
          create_agent(model, tools, middleware, system_prompt, state_schema)
```

简单说：body 参数决定"这次用什么模型、开什么功能"，config.yaml 提供"模型怎么连、工具有哪些、中间件配了什么"，两者合在一起决定创建出的 agent 长什么样

> 本步骤：创建 agent 的参数 = body 运行时参数 + config.yaml 静态配置。body 控制单次行为的开关，config.yaml 提供基础设施的定义，两者在 `make_lead_agent` 中合并后传给 `create_agent`
