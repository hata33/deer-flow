# 核心流程：从一条消息到多智能体协作

## 一句话本质

DeerFlow 的核心是一个 **"请求 → 构建 → 流式执行 → 事件推送"** 的管道。用户消息进入 Gateway API，触发 Agent 工厂按需构建一个带中间件链和工具集的 LangGraph 编译图，然后在一个 asyncio 协程中流式执行，通过 StreamBridge（asyncio.Queue）解耦生产和消费，SSE 推回前端。当任务复杂时，Lead Agent 通过 `task` 工具将子任务委派给 SubagentExecutor，在独立线程池中并发运行多个子智能体。

---

## 模块地图

```mermaid
flowchart TD
    subgraph L0["Layer 0: HTTP 入口"]
        R1["routers/runs.py\nPOST /api/runs/stream"]
        R2["routers/thread_runs.py\nPOST /{id}/runs/stream"]
    end

    subgraph SVC["services.py"]
        SR["start_run() 创建 RunRecord"]
        SC["sse_consumer() yield SSE 帧"]
        RAF["resolve_agent_factory()"]
    end

    R1 --> SR
    R2 --> SR

    subgraph L1["Layer 1: 运行时基础设施"]
        RM["RunManager\n进程级注册表"]
        SB["StreamBridge\nQueue 桥接"]
        CP["Checkpointer\n状态持久化"]
        WA["worker.py: run_agent()\n构建 Agent → astream → publish"]
    end

    SR --> RM
    SR --> SB
    SR --> WA

    subgraph L2["Layer 2: Agent 工厂"]
        MLA["make_lead_agent(config)"]
        MF["models/factory\ncreate_chat_model()"]
        TI["tools/__init__\nget_available_tools()"]
        MW2["_build_middlewares()"]
        PT["apply_prompt_template()"]

        MLA --> MF
        MLA --> TI
        MLA --> MW2
        MLA --> PT

        subgraph TOOLS["工具来源"]
            CT["config.yaml 定义的工具"]
            MT["MCP 工具"]
            BT["内置工具\npresent_file / ask_clarification / setup_agent"]
            VT["视觉工具\nview_image"]
            ST["子智能体工具\ntask"]
        end

        TI --> CT
        TI --> MT
        TI --> BT
        TI --> VT
        TI --> ST
    end

    WA --> MLA

    subgraph L3["Layer 3: 中间件链"]
        BA["before_agent: ThreadData → Uploads → Sandbox"]
        WM["wrap_model: DanglingTool → Guardrail → Summarize → Todo → ViewImage → DeferredFilter"]
        AM["after_model: TokenUsage → SubagentLimit → LoopDetect"]
        WT["wrap_tool: Guardrail → Audit → ErrorHandle → Clarify"]
        AA["after_agent: Title → Memory"]
    end

    subgraph L4["Layer 4: 子智能体执行"]
        TT2["task_tool.py → SubagentExecutor\nscheduler_pool + execution_pool\n每 5s 轮询 + stream_writer 推送进度"]
    end

    subgraph L5["Layer 5: 状态与持久化"]
        TS["ThreadState 运行时"]
        CK["Checkpoint 对话历史"]
        SS["Store 线程元数据"]
        MM["Memory 用户记忆"]
    end
```

---

## 请求路径跟踪：用户发一条消息到收到回答

下面以最常用的 `POST /api/runs/stream` 为例，逐层跟踪一个完整请求的旅程。

### 第 0 层：依赖初始化（应用启动时，一次性）

**文件**: `app/gateway/deps.py` → `app/gateway/app.py`

```mermaid
flowchart LR
    LS["FastAPI lifespan"] --> LR["langgraph_runtime(app)"]
    LR --> SB["make_stream_bridge()\napp.state.stream_bridge\n进程级单例"]
    LR --> CK["make_checkpointer()\napp.state.checkpointer\n持久化单例"]
    LR --> ST["make_store()\napp.state.store\nSQLite 单例"]
    LR --> RM["RunManager()\napp.state.run_manager\n进程级注册表"]
```

四个单例通过 `request.app.state` 在请求间共享。`deps.py` 中的 `get_xxx(request)` 函数负责提取，路由层调用它们获取依赖。

### 第 1 层：HTTP 入口

**文件**: `routers/runs.py:34`

```python
@router.post("/stream")
async def stateless_stream(body: RunCreateRequest, request: Request):
    thread_id = _resolve_thread_id(body)           # 从 body.config 取，或生成 UUID
    bridge = get_stream_bridge(request)             # 从 app.state 取单例
    run_mgr = get_run_manager(request)              # 从 app.state 取单例
    record = await start_run(body, thread_id, request)  # 启动运行
    return StreamingResponse(
        sse_consumer(bridge, record, request, run_mgr), # SSE 消费者
        media_type="text/event-stream",
    )
```

**路由层只做三件事**：解析 thread_id、启动运行、返回 SSE 流。业务逻辑全部委托给 `services.py`。

### 第 2 层：运行生命周期（start_run）

**文件**: `services.py:190-263`

```mermaid
flowchart TD
    START["start_run(body, thread_id, request)"]

    START --> D1["1. 获取依赖\nbridge / run_mgr / checkpointer / store"]
    D1 --> D2["2. run_mgr.create_or_reject()\n创建 RunRecord，冲突检测"]
    D2 --> D3["3. resolve_agent_factory()\n返回 make_lead_agent 函数引用"]
    D3 --> D4["4. normalize_input()\n把 messages 转成 HumanMessage"]
    D4 --> D5["5. build_run_config()\n构建 RunnableConfig"]
    D5 --> D6["6. asyncio.create_task(run_agent(...))\n注册后台协程，立即返回"]
    D6 --> D7["7. _sync_thread_title_after_run()\n后台同步标题（异步，不阻塞）"]
    D7 --> RET["返回 RunRecord"]

    D2 -.->|"reject → 409"| ERR["ConflictError"]
```

**关键设计**：`asyncio.create_task` 把 `run_agent` 注册到事件循环但不立即执行。此时同一个循环上挂着两个协程——`run_agent`（等调度）和 `sse_consumer`（等队列数据）——它们通过 asyncio.Queue 解耦。

### 第 3 层：Agent 执行（run_agent）

**文件**: `packages/harness/deerflow/runtime/runs/worker.py:26`

```mermaid
flowchart TD
    START["run_agent(bridge, run_manager, record, ...)"]

    START --> S1["1. run_manager.set_status(running)"]
    S1 --> S2["2. bridge.publish(metadata, {run_id, thread_id})"]
    S2 --> S3["3. 构建 Agent"]

    subgraph BUILD["构建 Agent"]
        B1["Runtime(context={thread_id}, store=store)"]
        B2["agent_factory(config)\n→ make_lead_agent(config)\n→ 返回编译图"]
        B3["agent.checkpointer = checkpointer"]
        B4["agent.interrupt_before/after = ..."]
        B1 --> B2 --> B3 --> B4
    end

    S3 --> S4["4. 构建 stream_mode 列表\nvalues / messages / updates"]
    S4 --> S5["5. 流式执行核心循环"]

    subgraph LOOP["astream 核心循环"]
        L1["async for chunk in agent.astream(...)"]
        L2["检查 abort_event（用户取消）"]
        L3["serialize(chunk, mode)"]
        L4["bridge.publish(run_id, event, data)"]
        L1 --> L2 --> L3 --> L4
    end

    S5 --> S6["6. 设置最终状态\nsuccess / interrupted / error"]
    S6 --> S7["7. finally: bridge.publish_end(run_id)\n发送 END_SENTINEL"]
```

**步骤 3 中 `agent_factory(config)` 触发完整的 Agent 构建**——这是最关键的一步，下面展开。

### 第 4 层：Agent 工厂（make_lead_agent）

**文件**: `packages/harness/deerflow/agents/lead_agent/agent.py:274`

```mermaid
flowchart TD
    MLA["make_lead_agent(config: RunnableConfig)"]

    MLA --> S1["1. 从 config.configurable 提取参数\nthinking_enabled, model_name,\nis_plan_mode, subagent_enabled, ..."]
    S1 --> S2["2. 三级优先级解析模型\n请求 model_name → agent_config.model → config.yaml models[0]"]
    S2 --> S3["3. 验证模型能力\nthinking 不支持 → 自动降级关闭"]
    S3 --> S4["4. 注入 LangSmith 追踪元数据"]
    S4 --> S5["5. create_agent(...) → CompiledStateGraph"]

    subgraph CREATE["create_agent 五大子系统"]
        C1["model = create_chat_model(name,\nthinking_enabled, reasoning_effort)"]
        C2["tools = get_available_tools(\nmodel_name, groups, subagent_enabled)"]
        C3["middleware = _build_middlewares(\nconfig, model_name)"]
        C4["system_prompt = apply_prompt_template(\nsubagent_enabled, ...)"]
        C5["state_schema = ThreadState"]
    end

    S5 --> CREATE
```

五个子系统的组装：

#### 4.1 模型工厂（create_chat_model）

**文件**: `packages/harness/deerflow/models/factory.py`

```mermaid
flowchart TD
    CCM["create_chat_model(name, thinking_enabled, reasoning_effort)"]
    CCM --> A["get_app_config() → 取模型配置"]
    A --> B["resolve_class(model_config.use)\n反射加载模型类\n如 langchain_openai:ChatOpenAI"]
    B --> C["构建 model 实例 + thinking 参数"]
    C --> D["返回 BaseChatModel"]
```

#### 4.2 工具组装（get_available_tools）

**文件**: `packages/harness/deerflow/tools/__init__.py`

```mermaid
flowchart TD
    GAT["get_available_tools(groups, include_mcp, model_name, subagent_enabled)"]

    GAT --> T1["1. config.yaml 定义的通用工具\nresolve_variable 动态加载"]
    GAT --> T2["2. 内置工具\npresent_file_tool / ask_clarification / setup_agent"]
    GAT --> T3["3. MCP 工具\n延迟加载，mtime 缓存失效"]
    GAT --> T4["4. ACP 工具\n外部智能体调用"]
    GAT --> T5["5. 条件工具\nview_image 仅 vision 模型\ntask 仅 subagent_enabled"]
    GAT --> T6["6. 社区工具\ntavily / jina_ai / firecrawl / image_search"]
```

#### 4.3 中间件链（_build_middlewares）

**文件**: `agent.py:209-271`

中间件按严格顺序组装，执行时依次经过。顺序不可随意调整，因为后续中间件依赖前置中间件设置的状态：

```mermaid
flowchart TD
    BM["_build_middlewares(config, model_name)"]

    subgraph BASE["基础运行时中间件 build_lead_runtime_middlewares"]
        direction TB
        M1["ThreadDataMiddleware — before_agent: 创建线程目录"]
        M2["UploadsMiddleware — before_agent: 注入上传文件"]
        M3["SandboxMiddleware — before_agent: 懒初始化沙箱"]
        M4["DanglingToolCallMiddleware — wrap_model_call: 修补缺失 ToolMessage"]
        M5["GuardrailMiddleware — wrap_tool_call: 工具调用授权"]
        M6["SandboxAuditMiddleware — wrap_tool_call: bash 命令审计"]
        M7["ToolErrorHandlingMiddleware — wrap_tool_call: 工具异常兜底"]
    end

    BM --> BASE

    BASE --> M8["SummarizationMiddleware\n条件: enabled\nwrap_model_call: 上下文压缩"]
    M8 --> M9["TodoListMiddleware\n条件: plan_mode\n注入 write_todos"]
    M9 --> M10["TokenUsageMiddleware\n条件: enabled\nafter_model: token 统计"]
    M10 --> M11["TitleMiddleware — 始终\nafter_agent: 自动生成标题"]
    M11 --> M12["MemoryMiddleware — 始终\nafter_agent: 排队记忆更新"]
    M12 --> M13["ViewImageMiddleware\n条件: vision\nwrap_model_call: 注入图片"]
    M13 --> M14["DeferredToolFilterMiddleware\n条件: tool_search\nwrap_model_call: 隐藏延迟工具"]
    M14 --> M15["SubagentLimitMiddleware\n条件: subagent\nafter_model: 截断超额 task 调用"]
    M15 --> M16["LoopDetectionMiddleware — 始终\nafter_model: 检测循环"]
    M16 --> M17["自定义 middlewares"]
    M17 --> M18["ClarificationMiddleware — 始终，最后\nwrap_tool_call: 拦截澄清请求"]
```

#### 4.4 系统提示（apply_prompt_template）

**文件**: `packages/harness/deerflow/agents/lead_agent/prompt.py`

```mermaid
flowchart TD
    APT["apply_prompt_template(subagent_enabled, max_concurrent_subagents, agent_name)"]

    APT --> P1["SOUL.md — Agent 人格与身份"]
    APT --> P2["Memory 注入\nget_memory_data → format_memory_for_injection\n顶部 15 条 facts + 用户上下文"]
    APT --> P3["Skills 列表\nload_skills → 格式化为可用技能说明"]
    APT --> P4["Subagent 编排指令\n子智能体使用规则（仅 subagent_enabled）"]
    APT --> P5["工作目录与文件管理规则"]
    APT --> P6["引用要求与研究指南"]

    P1 & P2 & P3 & P4 & P5 & P6 --> OUT["拼接为完整 system_prompt 字符串"]
```

#### 4.5 状态模式（ThreadState）

**文件**: `packages/harness/deerflow/agents/thread_state.py`

```mermaid
classDiagram
    class AgentState {
        +list messages
    }

    class ThreadState {
        +list~BaseMessage~ messages
        +SandboxState sandbox
        +ThreadDataState thread_data
        +str title
        +list~str~ artifacts
        +list todos
        +list~dict~ uploaded_files
        +dict viewed_images
    }

    class SandboxState {
        +str sandbox_id
    }

    class ThreadDataState {
        +str workspace
        +str uploads
        +str outputs
    }

    AgentState <|-- ThreadState : extends
    ThreadState *-- SandboxState
    ThreadState *-- ThreadDataState

    note for ThreadState "messages: append 驱动循环\nsandbox: 覆盖 SandboxMiddleware\nthread_data: 覆盖 ThreadDataMiddleware\ntitle: 覆盖 TitleMiddleware\nartifacts: 去重合并 present_files\ntodos: 覆盖 write_todos\nuploaded_files: 覆盖 UploadsMiddleware\nviewed_images: 字典合并 ViewImageMiddleware"
```

### 第 5 层：Agent Loop 执行

Agent 构建完成后，回到 `worker.py` 的 `agent.astream(graph_input, config, stream_mode)` 进入执行循环：

```mermaid
flowchart TD
    START(["Agent Loop 开始"]) --> BA["before_agent 钩子"]
    BA --> BA1["ThreadDataMiddleware → 创建线程目录"]
    BA1 --> BA2["UploadsMiddleware → 注入上传文件到 messages"]
    BA2 --> BA3["SandboxMiddleware → 获取沙箱，存 sandbox_id"]

    BA3 --> WM["wrap_model_call 钩子"]
    WM --> WM1["DanglingToolCallMiddleware → 修补缺失 ToolMessage"]
    WM1 --> WM2["SummarizationMiddleware → 接近上限时摘要旧消息"]
    WM2 --> WM3["ViewImageMiddleware → 注入图片 base64"]
    WM3 --> WM4["DeferredToolFilterMiddleware → 隐藏延迟工具 schema"]

    WM4 --> LLM["LLM 调用\ncreate_chat_model 返回的模型实例"]

    LLM --> AM["after_model 钩子"]
    AM --> AM1["TokenUsageMiddleware → 记录 token"]
    AM1 --> AM2["SubagentLimitMiddleware → 截断超额 task 调用"]
    AM2 --> AM3["LoopDetectionMiddleware → 检测循环"]

    AM3 --> TC{"有 tool_calls?"}

    TC -->|"是"| WT["wrap_tool_call 钩子（逐个执行）"]
    WT --> WT1["GuardrailMiddleware → 授权检查"]
    WT1 --> WT2["SandboxAuditMiddleware → bash 审计"]
    WT2 --> WT3["ToolErrorHandlingMiddleware → 异常兜底"]
    WT3 --> WT4["ClarificationMiddleware → 拦截 ask_clarification"]
    WT4 --> EXEC["执行工具 → 结果追加到 messages"]
    EXEC --> WM

    TC -->|"否"| AA["after_agent 钩子"]
    AA --> AA1["TitleMiddleware → 首次对话生成标题"]
    AA1 --> AA2["MemoryMiddleware → 排队异步记忆更新"]
    AA2 --> DONE(["循环结束，返回最终回答"])

    style LLM fill:#9cf,stroke:#333
    style DONE fill:#9f9,stroke:#333
    style TC fill:#ff9,stroke:#333
```

`messages` 列表驱动循环：LLM 输出追加到 messages，工具回复追加到 messages，直到 LLM 不再输出 `tool_calls`。

---

## 子智能体协作全流程

当一个复杂任务需要多智能体协作时，Lead Agent 调用 `task` 工具委派子任务。

### 触发条件

Lead Agent 在 `after_model` 阶段输出 `tool_calls` 包含 `task` 工具调用。`SubagentLimitMiddleware` 确保不超过 `max_concurrent_subagents`（默认 3）。

### 完整协作链路

```mermaid
flowchart TD
    INPUT["Lead Agent 输出 tool_calls: task(description, prompt, subagent_type)"]

    INPUT --> TT["task_tool.py: task_tool()"]

    TT --> V1["1. 校验子智能体类型\nget_subagent_config(subagent_type)\n内置: general-purpose / bash"]

    V1 --> V2["2. 构建子智能体工具集\nget_available_tools(subagent_enabled=False)\n排除 task，防止递归"]

    V2 --> V3["3. 创建 SubagentExecutor\n继承父级: sandbox_state, thread_data,\nthread_id, parent_model"]

    V3 --> V4["4. executor.execute_async(prompt, task_id)"]

    subgraph POOLS["双线程池执行"]
        direction TB
        SCHED["_scheduler_pool 3 workers"]
        EXEC_POOL["_execution_pool 3 workers"]
        SCHED --> EXEC_POOL

        subgraph AEXEC["asyncio.run 内部"]
            direction TB
            CA["_create_agent()\ncreate_chat_model + 过滤工具\n+ 子智能体中间件 + system_prompt"]
            BIS["_build_initial_state(prompt)\n继承 sandbox + thread_data"]
            ASTREAM["agent.astream(state, stream_mode=values)\n收集 AI 消息 → 提取最终回答"]
            CA --> BIS --> ASTREAM
        end

        EXEC_POOL --> AEXEC
    end

    V4 --> POOLS
    POOLS --> POLL["5. 后台轮询（5s 间隔）"]

    POLL --> CHECK1{"有新 AI 消息?"}
    CHECK1 -->|"是"| SW["stream_writer(task_running)\n前端实时看到进度"]
    CHECK1 --> CHECK2{"终态检查"}
    SW --> CHECK2

    CHECK2 -->|"COMPLETED"| OK["stream_writer(task_completed)\n返回结果"]
    CHECK2 -->|"FAILED"| FAIL["stream_writer(task_failed)\n返回错误"]
    CHECK2 -->|"TIMED_OUT"| TIMEOUT["stream_writer(task_timed_out)\n返回超时"]

    OK --> CLEAN["6. cleanup_background_task(task_id)\n防止内存泄漏"]
    FAIL --> CLEAN
    TIMEOUT --> CLEAN
```

### 并发子智能体架构

```mermaid
flowchart TD
    LA["Lead Agent\nasyncio 协程，主线程事件循环"]

    LA --> SA1["task_tool(研究量子计算)\ngeneral-purpose"]
    LA --> SA2["task_tool(搜索最新论文)\ngeneral-purpose"]
    LA --> SA3["task_tool(执行测试)\nbash"]

    SA1 --> SP1["_scheduler_pool"]
    SA2 --> SP2["_scheduler_pool"]
    SA3 --> SP3["_scheduler_pool"]

    SP1 --> EP1["_execution_pool 线程\nasyncio.run 独立事件循环\n独立 Agent（继承沙箱，不继承对话历史）"]
    SP2 --> EP2["_execution_pool 线程\nasyncio.run 独立事件循环\n独立 Agent"]
    SP3 --> EP3["_execution_pool 线程\nasyncio.run 独立事件循环\n独立 Agent"]

    subgraph LIMIT["SubagentLimitMiddleware 确保最多 3 个并发"]
        LIMIT_NOTE["超额的 task 调用在 after_model 阶段被截断"]
    end
```

**关键设计**：
- 子智能体在独立线程池中运行（`ThreadPoolExecutor`），与 Lead Agent 的 asyncio 事件循环隔离
- 子智能体通过 `asyncio.run()` 创建自己的事件循环，可以执行异步工具（MCP 工具等）
- 沙箱和线程目录从父级继承，保证文件系统路径一致
- `task_tool` 在 Lead Agent 协程中以 5 秒间隔轮询结果，同时通过 `stream_writer` 推送进度
- 双线程池架构：`_scheduler_pool`（3 workers）负责任务编排，`_execution_pool`（3 workers）负责实际执行

---

## 完整时序图：从一条消息到多智能体协作

```mermaid
sequenceDiagram
    participant FE as 前端
    participant GW as runs.py 路由
    participant SR as services.py
    participant RA as worker.py run_agent
    participant LA as make_lead_agent
    participant MW as 中间件链
    participant LLM as LLM 模型
    participant TT as task_tool
    participant SE as SubagentExecutor
    participant BR as StreamBridge

    FE->>GW: POST /api/runs/stream
    GW->>SR: start_run(body, thread_id)
    SR->>SR: RunManager.create_or_reject()
    SR->>RA: asyncio.create_task(run_agent)
    GW-->>FE: 200 SSE 流开始

    Note over RA: 事件循环调度 run_agent
    RA->>LA: agent_factory(config)
    LA->>LA: create_chat_model()
    LA->>LA: get_available_tools()
    LA->>LA: _build_middlewares()
    LA->>LA: apply_prompt_template()
    LA-->>RA: CompiledStateGraph

    RA->>BR: publish("metadata", {run_id})
    RA->>MW: before_agent 钩子
    MW->>MW: ThreadData + Uploads + Sandbox

    RA->>MW: wrap_model_call → LLM
    MW->>LLM: 模型调用
    LLM-->>MW: tool_calls: task("研究", "...", "general-purpose")

    RA->>MW: after_model 钩子
    MW->>MW: SubagentLimit → LoopDetection

    RA->>MW: wrap_tool_call → task_tool
    MW->>TT: task_tool(description, prompt, subagent_type)
    TT->>SE: SubagentExecutor.execute_async(prompt)

    Note over SE: 独立线程池执行
    SE->>SE: create_agent + astream
    SE-->>SE: 子智能体完成

    TT->>BR: stream_writer("task_running" / "task_completed")
    BR-->>FE: SSE 事件推送

    TT-->>MW: "Task Succeeded. Result: ..."
    MW-->>RA: ToolMessage 追加到 messages

    RA->>MW: wrap_model_call → LLM（第二轮）
    MW->>LLM: 带工具结果的上下文
    LLM-->>MW: "根据研究结果..."（无 tool_calls）

    RA->>MW: after_agent 钩子
    MW->>MW: Title + Memory

    RA->>BR: publish("values", 最终状态)
    RA->>BR: publish_end() → END_SENTINEL
    BR-->>FE: event: end
```

---

## 沙箱与文件系统隔离

每个线程拥有独立的文件目录，Agent 看到的是虚拟路径：

| Agent 视角（虚拟路径） | 物理路径 |
| - | - |
| `/mnt/user-data/workspace/` | `.deer-flow/threads/{thread_id}/user-data/workspace/` |
| `/mnt/user-data/uploads/` | `.deer-flow/threads/{thread_id}/user-data/uploads/` |
| `/mnt/user-data/outputs/` | `.deer-flow/threads/{thread_id}/user-data/outputs/` |
| `/mnt/skills/` | `deer-flow/skills/` |
| `/mnt/acp-workspace/` | `.deer-flow/threads/{thread_id}/acp-workspace/` |

Sandbox 工具（bash / read_file / write_file / ls / str_replace）通过 `replace_virtual_path()` 在执行时翻译路径。`mask_local_paths_in_output()` 确保输出中不泄露宿主机物理路径。

---

## 状态持久化层次

| 组件 | 存储 | 生命周期 | 内容 |
|------|------|---------|------|
| `StreamBridge` | asyncio.Queue（内存） | 进程重启丢失 | SSE 事件流 |
| `RunManager` | dict（内存） | 进程重启丢失 | 运行状态注册表 |
| `Checkpointer` | 磁盘/SQLite | 持久 | 完整对话历史（messages + 所有 ThreadState 字段） |
| `Store` | SQLite | 持久 | 线程元数据（标题、创建时间） |
| `Memory` | memory.json | 持久 | 用户画像、事实、历史上下文 |

---

## 关键设计决策总结

| 设计 | 原因 |
|------|------|
| StreamBridge 解耦生产消费 | Agent 执行不被网络推送阻塞，SSE 推送不被 Agent 阻塞 |
| 每次请求构建新 Agent | 支持运行时参数切换（模型、工具、中间件），无需预热 |
| 子智能体在独立线程池 | 与主事件循环隔离，避免阻塞其他请求的 SSE 推送 |
| task_tool 轮询 + stream_writer | Lead Agent 不需要额外查询接口，前端通过同一 SSE 流接收子智能体进度 |
| 双线程池（scheduler + execution） | 调度编排与实际执行分离，避免调度器阻塞 |
| 中间件严格顺序 | 后续中间件依赖前置中间件设置的状态（如 SandboxMiddleware 设置 sandbox_id 后工具才能使用沙箱） |
| 虚拟路径系统 | Agent 看到一致的路径，不感知宿主机物理路径差异 |
