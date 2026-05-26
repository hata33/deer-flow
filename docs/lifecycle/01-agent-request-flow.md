# Agent 请求全流程

> 端到端追踪一次完整的用户请求：从 HTTP 到达到 SSE 流式响应，穿越 Gateway、RunManager、LangGraph、最多 20 个中间件（其中 8 个基础必选 + 最多 12 个按条件加载）、LLM 调用、工具执行、前端渲染的完整路径。

---

## 全链路架构图

```
┌──────────┐  HTTP   ┌──────────┐  create  ┌──────────────┐  astream  ┌──────────────┐
│ Frontend │ ──────▸ │ Gateway  │ ───────▸ │ RunManager   │ ────────▸ │ LangGraph    │
│ (3000)   │ ◂────── │ API      │          │ (worker.py)  │           │ Graph        │
└──────────┘  SSE    └──────────┘          └──────────────┘           └───────┬──────┘
     ▲                                        run_agent()                     │
     │                                            │                           ▼
     │                                            ▼                   ┌──────────────┐
     │                                     ┌──────────────┐           │ Agent Node   │
     │                                     │ StreamBridge │ ◂──────── │ (middleware  │
     │                                     │ (SSE events) │           │  chain)      │
     │                                     └──────────────┘           └──────┬───────┘
     │                                                                       │
     │                                            ┌──────────────────────────┤
     │                                            ▼                          ▼
     │                                     ┌─────────────┐         ┌─────────────┐
     └──────────────────────────────────── │ LLM Model   │         │ Tool Call   │
       SSE: messages, custom, end          │ (bind_tools)│         │ (bash/ls/…) │
                                           └─────────────┘         └─────────────┘
```

---

## 阶段 ①：HTTP 请求到达

**入口文件**: `app/gateway/routers/thread_runs.py` → `stream_run()`

```
POST /api/threads/{thread_id}/runs/stream
Content-Type: application/json

{
  "input": { "messages": [{"role": "user", "content": "你好"}] },
  "config": { "configurable": { "model_name": null } }
}
```

**路由层**:
1. `stream_run()` 接收 `RunCreateRequest`（包含用户消息和运行配置）
2. 调用 `start_run()` 初始化运行
3. 返回 `StreamingResponse`，Content-Type 为 `text/event-stream`

**跨模块协作**:
- **Gateway ↔ RunManager**: Gateway 调用 `run_manager.create_or_reject()` 处理并发策略（interrupt/rollback）

---

## 阶段 ②：RunManager 创建运行

**核心文件**: `app/gateway/services.py` → `start_run()`

```
start_run()
  ├─ 1. 模型白名单校验（model_name vs config.models）
  ├─ 2. run_manager.create_or_reject()           处理并发策略
  │     └─ multitask_strategy: "interrupt" | "rollback"
  ├─ 3. 创建/验证线程元数据（thread_meta）
  ├─ 4. 构建运行配置
  │     ├─ 注入 user_id（从认证上下文或 "default"）
  │     ├─ 注入 thread_id
  │     └─ 注入 configurable（model_name, thinking_enabled 等）
  └─ 5. 创建异步任务 → run_agent()
```

**跨模块协作**:
- **RunManager ↔ Config**: 读取 `config.yaml` 获取模型白名单
- **RunManager ↔ UserContext**: 通过 `get_effective_user_id()` 解析用户身份
- **RunManager ↔ Persistence**: 运行状态持久化到 RunStore（如果配置了）

---

## 阶段 ③：Agent 执行引擎

**核心文件**: `deerflow/runtime/runs/worker.py` → `run_agent()`

```
run_agent()
  ├─ 1. 设置运行状态为 running
  ├─ 2. 捕获 pre-run checkpoint（用于 rollback 回退）
  ├─ 3. 发布 metadata 事件（run_id, thread_id）
  ├─ 4. 调用 agent_factory() → make_lead_agent()    创建 Agent 实例
  ├─ 5. 配置 LangGraph 流模式
  │     ├─ "values"       完整状态快照
  │     ├─ "messages"     消息增量
  │     └─ "custom"       自定义事件（子代理进度等）
  ├─ 6. agent.astream(input, config, stream_mode)
  └─ 7. 遍历流事件 → StreamBridge.publish()
```

**跨模块协作**:
- **Worker ↔ Agent Factory**: 每次请求创建新的 Agent 实例（或复用缓存的）
- **Worker ↔ StreamBridge**: 所有流事件通过 StreamBridge（抽象基类，实现类如 `MemoryStreamBridge`）发布给 SSE 消费者

---

## 阶段 ④：Agent 构建

**核心文件**: `deerflow/agents/lead_agent/agent.py` → `make_lead_agent()` → `_make_lead_agent()`

> 注: `make_lead_agent()` 是 LangGraph Server 入口（保持签名兼容），内部委托给 `_make_lead_agent()` 执行实际构建。

```
make_lead_agent(config)
  └─ _make_lead_agent(config, app_config)
       ├─ 1. 模型解析（_resolve_model_name）
       │     configurable.model_name → 回退到 config.models[0]
       ├─ 2. create_chat_model()                          创建 LLM 实例
       │     └─ 支持thinking/vision/Responses API
       ├─ 3. get_available_tools()                        加载所有工具
       │     ├─ config.yaml 定义的社区工具（resolve_variable 反射加载）
       │     ├─ MCP 工具（懒初始化 + mtime 缓存）
       │     ├─ 内置工具（present_files, ask_clarification, view_image, task）
       │     └─ 工具过滤（skill allowed-tools 白名单）
       ├─ 4. _build_middlewares()                         构建中间件链
       │     ├─ ① build_lead_runtime_middlewares()    8 个基础中间件
       │     └─ ② 按条件追加最多 12 个中间件
       └─ 5. create_react_agent(model, tools, prompt, middlewares)
```

**跨模块协作**:
- **Agent Factory ↔ Models**: 通过 `create_chat_model()` 反射创建 LLM
- **Agent Factory ↔ Tools**: 通过 `resolve_variable()` 动态加载社区工具
- **Agent Factory ↔ Skills**: 通过 `filter_tools_by_skill_allowed_tools()` 裁剪工具列表
- **Agent Factory ↔ MCP**: 懒加载 MCP 工具，mtime 变化时自动失效
- **Agent Factory ↔ Prompt**: 通过 `apply_prompt_template()` 生成系统提示词

---

## 阶段 ⑤：中间件链执行

中间件分两层装配：先由 `build_lead_runtime_middlewares()`（在 `tool_error_handling_middleware.py` 中）构建 8 个基础必选中间件，再由 `_build_middlewares()`（在 `agent.py` 中）按条件追加最多 12 个中间件，最后以 `ClarificationMiddleware` 收尾。

**实际装配顺序**（来源: `agent.py:291-376` + `tool_error_handling_middleware.py:70-126`）：

```
① build_lead_runtime_middlewares() — 基础运行时（8 个，必选）
   [1]  ThreadDataMiddleware          创建线程目录
   [2]  UploadsMiddleware             注入上传文件元数据
   [3]  SandboxMiddleware             获取沙箱实例
   [4]  DanglingToolCallMiddleware    修补悬挂的工具调用
   [5]  LLMErrorHandlingMiddleware    LLM 调用错误处理
   [6]  GuardrailMiddleware           工具调用前置授权 (可选, guardrails.enabled)
   [7]  SandboxAuditMiddleware        Bash 命令安全审计
   [8]  ToolErrorHandlingMiddleware   工具异常转错误 ToolMessage

② _build_middlewares() 追加 — 按条件加载（最多 12 个）
   [9]  DynamicContextMiddleware      注入记忆+日期 (必选)
   [10] SummarizationMiddleware       token 接近上限时压缩上下文 (可选, summarization.enabled)
   [11] TodoMiddleware                任务追踪 (可选, is_plan_mode)
   [12] TokenUsageMiddleware          Token 用量统计 (可选, token_usage.enabled)
   [13] TitleMiddleware               自动标题生成 (必选)
   [14] MemoryMiddleware              记忆更新排队 (必选)
   [15] ViewImageMiddleware           图像内容注入 (可选, supports_vision)
   [16] DeferredToolFilterMiddleware  延迟工具过滤 (可选, tool_search.enabled)
   [17] SubagentLimitMiddleware       子代理并发限制 (可选, subagent_enabled)
   [18] LoopDetectionMiddleware       循环检测 (可选, loop_detection.enabled)
   [19] custom_middlewares            自定义中间件 (可选, 用户注入)
   [20] ClarificationMiddleware       澄清拦截 (必选, 始终最后)
```

中间件在不同生命周期钩子中执行：

```
请求消息进入
     │
     ▼
┌─ before_agent ──────────────────────────────────────────────┐
│ [1]  ThreadDataMiddleware    创建线程目录                      │
│ [2]  UploadsMiddleware       注入上传文件元数据                │
│ [3]  SandboxMiddleware       获取沙箱实例                      │
│ [9]  DynamicContextMiddleware 注入记忆+日期到 HumanMessage     │
└─────────────────────────────────────────────────────────────┘
     │
     ▼
┌─ before_model ──────────────────────────────────────────────┐
│ [10] SummarizationMiddleware  token 接近上限时压缩上下文       │
│ [11] TodoMiddleware           任务上下文丢失检测               │
│ [15] ViewImageMiddleware      注入 base64 图像数据            │
│ [16] DeferredToolFilterMiddleware 移除延迟工具 schema         │
└─────────────────────────────────────────────────────────────┘
     │
     ▼
┌─ wrap_model_call ───────────────────────────────────────────┐
│ [4]  DanglingToolCallMiddleware  修补悬挂的工具调用            │
│ [5]  LLMErrorHandlingMiddleware  LLM 调用错误处理             │
└─────────────────────────────────────────────────────────────┘
     │
     ▼
┌─ LLM 调用 ─────────────────────────────────────────────────┐
│  model.bind_tools(tools).invoke(messages)                  │
│  → 返回 AIMessage（可能包含 tool_calls）                     │
└─────────────────────────────────────────────────────────────┘
     │
     ▼
┌─ after_model ───────────────────────────────────────────────┐
│ [12] TokenUsageMiddleware     记录 token 用量                 │
│ [13] TitleMiddleware          首次交换后生成标题               │
│ [17] SubagentLimitMiddleware  截断超出限制的 task 调用         │
│ [18] LoopDetectionMiddleware  检测并打断循环                   │
│ [11] TodoMiddleware           防提前退出（未完成任务时提醒）    │
└─────────────────────────────────────────────────────────────┘
     │
     ▼ (如果 AIMessage 包含 tool_calls)
┌─ wrap_tool_call ────────────────────────────────────────────┐
│ [6]  GuardrailMiddleware      工具调用前置授权                 │
│ [8]  ToolErrorHandlingMiddleware 工具异常转错误 ToolMessage   │
│ [7]  SandboxAuditMiddleware   Bash 命令安全审计               │
│ [20] ClarificationMiddleware  拦截 ask_clarification          │
└─────────────────────────────────────────────────────────────┘
     │
     ▼
┌─ 工具执行 ──────────────────────────────────────────────────┐
│  LangChain 执行工具 → 返回 ToolMessage                      │
│  → 回到 before_model（循环直到无 tool_calls）                │
└─────────────────────────────────────────────────────────────┘
     │
     ▼ (无 tool_calls，最终响应)
┌─ after_agent ───────────────────────────────────────────────┐
│ [14] MemoryMiddleware         排队记忆更新                   │
└─────────────────────────────────────────────────────────────┘
```

---

## 阶段 ⑥：SSE 流式传输

**核心文件**: `app/gateway/services.py` → `sse_consumer()`

```
StreamBridge 事件流
  ├─ metadata       {run_id, thread_id}
  ├─ values         完整状态快照（title, messages, artifacts）
  ├─ messages-tuple 消息增量（AI delta, tool call, tool result）
  ├─ custom         子代理事件（task_started, task_running 等）
  └─ end            流终止（携带 usage 统计）

  ↓ format_sse()

SSE 格式:
  event: values
  data: {"title": "...", "messages": [...]}

  event: messages-tuple
  data: [{"id": "msg_xxx", "type": "ai", "content": "你"}]

  event: custom
  data: {"type": "task_started", "task_id": "...", "description": "..."}

  event: end
  data: {"usage": {"input_tokens": 123, "output_tokens": 456}}
```

---

## 阶段 ⑦：前端接收与渲染

**核心文件**: `frontend/src/core/threads/hooks.ts` → `useThreadStream()`

```
useThreadStream()
  ├─ 使用 LangGraph SDK 的 useStream()
  ├─ 事件处理:
  │   ├─ onCreated         设置 thread_id
  │   ├─ onLangChainEvent  工具完成事件
  │   ├─ onUpdateEvent     消息更新 + 标题变化
  │   ├─ onCustomEvent     子代理进度事件
  │   ├─ onError           显示错误提示
  │   └─ onFinish          更新 UI 状态
  ├─ 消息去重: 按 message.id 合并 delta（同一个 id 的 delta 拼接为完整消息）
  ├─ 摘要消息: name="summary" 的消息对用户隐藏（summarizedRef 追踪）
  └─ 乐观消息: 先显示用户输入，流式合并 AI 响应
```

**跨模块协作**:
- **Frontend ↔ Gateway**: 通过 `/api/langgraph/*` 路由（nginx 代理）
- **Frontend ↔ StreamBridge**: SSE 协议，单向流
- **Frontend ↔ Summarization**: 隐藏 name="summary" 消息

---

## 跨模块协作汇总

```
Gateway ──创建──▸ RunManager ──调用──▸ Worker
    │                                   │
    │         ┌─────────────────────────┤
    │         ▼                         ▼
    │    Agent Factory            StreamBridge
    │    ├─ Models ◂──── Config         │
    │    ├─ Tools  ◂──── Skills         │
    │    ├─ MCP    ◂──── ExtensionsConfig
    │    └─ Prompt ◂──── Memory         │
    │         │                         │
    │         ▼                         ▼
    │    Middleware Chain          SSE Events
    │    ├─ 最多 20 个中间件             │
    │    │  (8 基础必选 + 最多 12 条件)
    │    └─ 跨中间件协作:                │
    │       Memory↔Summarization        │
    │       DynamicContext↔Summarization
    │       Todo↔Summarization          │
    │       TokenUsage↔Subagent         │
    │                                   │
    │                                   ▼
    └──────────────────────────── Frontend (React)
```

---

## 深入阅读

| 模块 | 文档 |
|------|------|
| Agent 系统架构 | [docs/core/agent/01-overview.md](../core/agent/01-overview.md) |
| Agent 生命周期 | [docs/core/agent/02-lifecycle.md](../core/agent/02-lifecycle.md) |
| 中间件详解 | [docs/core/agent/05-middlewares.md](../core/agent/05-middlewares.md) |
| 运行时 | [docs/core/runtime/02-run-lifecycle.md](../core/runtime/02-run-lifecycle.md) |
| 事件流 | [docs/core/runtime/05-event-streaming.md](../core/runtime/05-event-streaming.md) |
| 配置系统 | [docs/core/config/00-overview.md](../core/config/00-overview.md) |
| Gateway API | [docs/core/gateway/00-overview.md](../core/gateway/00-overview.md) |
