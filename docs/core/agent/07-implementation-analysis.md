# 07 - Agent 系统实现机制深度分析

> 本文档基于 `backend/packages/harness/deerflow/agents/` 目录下的源码，逐层拆解 Agent 系统的实现细节。回答的是"代码怎么写的、为什么这么写"。

---

## 一、模块依赖关系图

```
┌──────────────────────────────────────────────────────────────────────┐
│                        调用方（外部世界）                              │
│                                                                       │
│  langgraph.json                        DeerFlowClient                │
│  ┌────────────────────┐                ┌──────────────────────┐     │
│  │ make_lead_agent()  │                │ create_deerflow_     │     │
│  │ (LangGraph Server) │                │ agent() (嵌入式)      │     │
│  └─────────┬──────────┘                └──────────┬───────────┘     │
└────────────┼──────────────────────────────────────┼────────────────┘
             │                                      │
             │  两级工厂                              │  SDK 级工厂
             ▼                                      ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       agents 包（内部世界）                             │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ lead_agent/                                                     │  │
│  │  agent.py ─── make_lead_agent() / _make_lead_agent()           │  │
│  │    ├─ _get_runtime_config()    合并 configurable + context     │  │
│  │    ├─ _resolve_model_name()    请求 → Agent配置 → 全局默认      │  │
│  │    ├─ _build_middlewares()     组装中间件链（~20个）              │  │
│  │    └─ apply_prompt_template()  构建静态系统提示词                │  │
│  │  prompt.py ─── 系统提示词模板 + 技能缓存管理                      │  │
│  └────────────────────────────┬───────────────────────────────────┘  │
│                               │                                       │
│  ┌────────────────────────────▼───────────────────────────────────┐  │
│  │ factory.py ─── create_deerflow_agent()                         │  │
│  │   └─ _assemble_from_features()  特性驱动的中间件组装             │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ middlewares/ ─── ~20个中间件实现                                 │  │
│  │   __init__.py ─── 顺序文档 + 子包入口                            │  │
│  │   tool_error_handling_middleware.py ─── 运行时中间件构建器        │  │
│  │     ├─ build_lead_runtime_middlewares()   Lead Agent 基础链      │  │
│  │     ├─ build_subagent_runtime_middlewares() 子代理基础链        │  │
│  │     └─ _build_runtime_middlewares()       共享基础              │  │
│  │   clarification_middleware.py ─── 澄清拦截（始终最后）            │  │
│  │   loop_detection_middleware.py ─── 循环检测 + 强制停止           │  │
│  │   dynamic_context_middleware.py ─── 记忆/日期动态注入            │  │
│  │   deferred_tool_filter_middleware.py ─── 延迟工具过滤           │  │
│  │   + memory / summarization / title / todo / token / vision...  │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌───────────────────┐  ┌───────────────────┐                       │
│  │ thread_state.py   │  │ features.py       │                       │
│  │ ThreadState schema│  │ RuntimeFeatures   │                       │
│  │ + reducers        │  │ @Next/@Prev 装饰器│                       │
│  └───────────────────┘  └───────────────────┘                       │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 二、第 1 层：中间件组装 — 两层工厂模式

### 2.1 入口：`_build_middlewares()`

**文件**：`agents/lead_agent/agent.py`

```python
def _build_middlewares(config, model_name, agent_name=None,
                       custom_middlewares=None, *, app_config):
    # ① 基础运行时中间件（共享层）
    middlewares = build_lead_runtime_middlewares(
        app_config=resolved_app_config, lazy_init=True)

    # ② DynamicContextMiddleware — 记忆/日期注入
    middlewares.append(DynamicContextMiddleware(...))

    # ③ SummarizationMiddleware — 按配置条件添加
    if summarization_middleware is not None:
        middlewares.append(summarization_middleware)

    # ④ TodoMiddleware — is_plan_mode 控制
    # ⑤ TokenUsageMiddleware — token_usage.enabled 控制
    # ⑥ TitleMiddleware — 始终添加
    # ⑦ MemoryMiddleware — 在 TitleMiddleware 之后
    # ⑧ ViewImageMiddleware — supports_vision 控制
    # ⑨ DeferredToolFilterMiddleware — tool_search.enabled 控制
    # ⑩ SubagentLimitMiddleware — subagent_enabled 控制
    # ⑪ LoopDetectionMiddleware — loop_detection.enabled 控制

    # ⑫ 自定义中间件 — 在 ClarificationMiddleware 之前
    if custom_middlewares:
        middlewares.extend(custom_middlewares)

    # ⑬ ClarificationMiddleware — 始终最后
    middlewares.append(ClarificationMiddleware())
    return middlewares
```

### 2.2 共享层：`build_lead_runtime_middlewares()`

**文件**：`agents/middlewares/tool_error_handling_middleware.py`

```
build_lead_runtime_middlewares()
  └─ _build_runtime_middlewares(
       include_uploads=True,           ← Lead Agent 需要上传
       include_dangling_tool_call=True  ← Lead Agent 需要悬挂修补
     )
```

```
build_subagent_runtime_middlewares()
  └─ _build_runtime_middlewares(
       include_uploads=False,          ← 子代理无上传
       include_dangling_tool_call=True
     )
     + ViewImageMiddleware (条件性)
```

共享层组装的固定顺序：

| 槽位 | 中间件 | 钩子作用 |
|------|--------|----------|
| [0] | ThreadDataMiddleware | `before_agent` — 创建线程目录 |
| [1] | UploadsMiddleware | `before_agent` — 注入上传文件 |
| [2] | SandboxMiddleware | `before_agent` — 获取沙箱 |
| [3] | DanglingToolCallMiddleware | `before_agent` — 修补悬挂工具调用 |
| [4] | LLMErrorHandlingMiddleware | `wrap_model_call` — LLM 错误重试/熔断 |
| [5] | GuardrailMiddleware（可选） | `wrap_tool_call` — 安全评估 |
| [6] | SandboxAuditMiddleware | `wrap_tool_call` — 命令审计 |
| [7] | ToolErrorHandlingMiddleware | `wrap_tool_call` — 异常兜底 |

---

## 三、第 2 层：钩子执行顺序与生命周期

LangGraph 的 `create_agent` 内置 ReAct 循环，中间件在以下五个钩子中执行：

```
用户消息进入
  │
  ▼
┌──────────────────────────────────────────────────────────────┐
│ before_agent (全部中间件，按链顺序)                             │
│   ThreadData → Uploads → Sandbox → DanglingToolCall → ...    │
│   DynamicContext → Memory → ViewImage                         │
└──────────────────────────┬───────────────────────────────────┘
                           │
              ┌────────────▼────────────────────────┐
              │ wrap_model_call (全部中间件)          │
              │   LLMErrorHandling → DeferredFilter  │
              │   → model.invoke() → 返回 AIMessage  │
              └────────────┬────────────────────────┘
                           │
              ┌────────────▼────────────────────────┐
              │ after_model (全部中间件)              │
              │   LoopDetection → SubagentLimit      │
              │   → TokenUsage                       │
              └────────────┬────────────────────────┘
                           │
            ┌──────────────▼──────────────────────────┐
            │ AIMessage 有 tool_calls ?                │
            │   YES → 进入工具执行                      │
            │   NO  → 跳到 after_agent                 │
            └──────────────┬──────────────────────────┘
                           │ (YES)
              ┌────────────▼────────────────────────┐
              │ wrap_tool_call (每个工具调用)         │
              │   Guardrail → SandboxAudit           │
              │   → ToolErrorHandling → handler()    │
              │   → Clarification (ask_clarification)│
              └────────────┬────────────────────────┘
                           │
              ┌────────────▼────────────────────────┐
              │ 回到 wrap_model_call（下一轮 ReAct）  │
              └──────────────────────────────────────┘

用户消息回答完成
  │
  ▼
┌──────────────────────────────────────────────────────────────┐
│ after_agent (全部中间件，按链顺序)                              │
│   Title → Memory → ...                                       │
└──────────────────────────────────────────────────────────────┘
```

---

## 四、第 3 层：状态模式 — ThreadState extends AgentState

**文件**：`agents/thread_state.py`

```
AgentState (LangChain 内置)
  ├─ messages: Annotated[list, add_messages]   ← 自动消息合并
  │
  └─ ThreadState (DeerFlow 扩展)
      ├─ sandbox: NotRequired[SandboxState]        ← SandboxMiddleware 写入
      ├─ thread_data: NotRequired[ThreadDataState]  ← ThreadDataMiddleware 写入
      ├─ title: NotRequired[str]                    ← TitleMiddleware 写入
      ├─ artifacts: Annotated[list[str], merge_artifacts]  ← present_files 写入
      ├─ todos: NotRequired[list]                   ← TodoMiddleware 写入
      ├─ uploaded_files: NotRequired[list[dict]]    ← UploadsMiddleware 写入
      └─ viewed_images: Annotated[dict, merge_viewed_images] ← view_image 写入
```

**Reducer 设计**：

| 字段 | Reducer | 行为 |
|------|---------|------|
| `artifacts` | `merge_artifacts` | 合并 + `dict.fromkeys` 去重 + 保持顺序 |
| `viewed_images` | `merge_viewed_images` | 合并字典，新值覆盖；空字典 `{}` 清除所有 |
| `messages` | `add_messages` (内置) | 按 ID 原位替换或追加 |

**为什么 `viewed_images` 有特殊清除逻辑**：`ViewImageMiddleware` 在 `before_agent` 中将 base64 图像数据注入 AIMessage 后，通过返回空字典 `{}` 清除 `viewed_images` 状态。这防止图像数据在后续轮次中被重复注入，同时保持状态干净。

---

## 五、第 4 层：工具绑定流程

### 5.1 完整装配管线

```
make_lead_agent()
  │
  ├─ get_available_tools(
  │     model_name, groups=agent_config.tool_groups,
  │     subagent_enabled, app_config
  │   )
  │   │
  │   ├─ ① config.yaml tools → resolve_variable(cfg.use) → Tool 实例
  │   ├─ ② 内置工具 (present_files, ask_clarification, view_image, ...)
  │   ├─ ③ MCP 工具 (从缓存加载)
  │   ├─ ④ ACP 工具 (条件性)
  │   └─ ⑤ 合并去重 (config > builtins > MCP > ACP)
  │
  ├─ filter_tools_by_skill_allowed_tools(
  │     tools, skills_for_tool_policy
  │   )
  │   │
  │   └─ 按技能的 allowed-tools 白名单过滤
  │      如果技能没有 allowed-tools 字段 → 不做过滤
  │
  └─ create_agent(
       model=..., tools=filtered_tools,
       middleware=..., system_prompt=...,
       state_schema=ThreadState
     )
```

### 5.2 `_ensure_sync_invocable_tool()` 的作用

某些调用路径（如嵌入式 DeerFlowClient）运行在同步上下文中，通过 `tool.func` 调用工具。但 MCP 工具和 ACP 工具只有 `coroutine`（异步实现）。`_ensure_sync_invocable_tool()` 检测这种情况，用 `make_sync_tool_wrapper()` 自动生成同步包装器。

---

## 六、第 5 层：提示词模板构建

### 6.1 `apply_prompt_template()` — 全静态设计

**文件**：`agents/lead_agent/prompt.py`

```python
def apply_prompt_template(subagent_enabled, max_concurrent_subagents, *,
                          agent_name, available_skills, app_config) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        agent_name=agent_name or "DeerFlow 2.0",
        soul=get_agent_soul(agent_name),         # 自定义 Agent 的 SOUL.md
        self_update_section=...,                  # update_agent 指令
        skills_section=...,                       # 可用技能列表
        deferred_tools_section=...,               # 延迟工具名列表
        subagent_section=...,                     # 子代理系统提示
        subagent_reminder=...,                    # 并发限制提醒
        subagent_thinking=...,                    # 分解检查引导
        acp_section=...,                          # ACP Agent 路径
    )
```

**关键约束**：模板中**没有** `{memory}` 或 `{current_date}` 占位符。这两个值通过 `DynamicContextMiddleware` 在运行时作为 `<system-reminder>` 注入到第一条 HumanMessage 中。

### 6.2 技能缓存机制

```python
# 模块导入时触发后台线程加载
prime_enabled_skills_cache()

# 版本化失效
_enabled_skills_refresh_version += 1  # 技能变更时递增

# per-config 缓存（避免重复扫描文件系统）
get_enabled_skills_for_config(app_config) → 按 id(app_config) 缓存

# lru_cache 格式化缓存
_get_cached_skills_prompt_section(skill_signature, ...) → 缓存格式化后的 XML
```

技能列表从磁盘加载是 I/O 操作。`prime_enabled_skills_cache()` 在模块导入时启动后台线程预加载，确保请求路径无阻塞 I/O。

### 6.3 子代理提示词动态构建

`_build_subagent_section(max_concurrent)` 动态生成包含并发限制参数的子代理系统提示：
- `MAX_CONCURRENT_SUBAGENTS = {n}` 硬限制
- 多批次执行策略（超出限制时分批）
- 可用子代理列表（从 `subagents` 注册表动态获取）
- 正反例对比说明

---

## 七、文件职责速查表

| 文件 | 核心职责 | 关键类/函数 |
|------|----------|------------|
| `lead_agent/agent.py` | Agent 工厂 + 中间件组装 | `make_lead_agent()`, `_build_middlewares()` |
| `lead_agent/prompt.py` | 系统提示词 + 技能缓存 | `apply_prompt_template()`, `SYSTEM_PROMPT_TEMPLATE` |
| `factory.py` | SDK 级纯参数工厂 | `create_deerflow_agent()`, `_assemble_from_features()` |
| `features.py` | 特性标志 + 定位装饰器 | `RuntimeFeatures`, `@Next`, `@Prev` |
| `thread_state.py` | 状态模式 + Reducer | `ThreadState`, `merge_artifacts`, `merge_viewed_images` |
| `middlewares/__init__.py` | 顺序文档 | 中间件顺序编号列表 |
| `middlewares/tool_error_handling_middleware.py` | 运行时中间件构建器 | `build_lead_runtime_middlewares()`, `build_subagent_runtime_middlewares()` |
| `middlewares/clarification_middleware.py` | 澄清拦截 | `ClarificationMiddleware._handle_clarification()` |
| `middlewares/loop_detection_middleware.py` | 循环检测 | `LoopDetectionMiddleware._track_and_check()` |
| `middlewares/dynamic_context_middleware.py` | 记忆/日期注入 | `DynamicContextMiddleware._inject()` |
| `middlewares/deferred_tool_filter_middleware.py` | 延迟工具过滤 | `DeferredToolFilterMiddleware._filter_tools()` |
