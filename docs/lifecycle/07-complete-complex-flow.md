# 完整复杂场景全链路追踪

> 以一个**最复杂的完整场景**为主线，事无巨细地追踪代码执行路径，覆盖前端 → 后端路由 → Service → StreamBridge → Agent创建 → 记忆加载 → LLM调用 → 子Agent沙箱执行 → 工具报错 → Human-in-the-loop中断 → 用户确认恢复 → 持久化 → 追踪 → 流式输出的全流程。

---

## 场景定义

**用户操作**：在"ultra"模式下发起一个研究任务：
> "帮我分析这个CSV数据文件，需要清洗、统计分析、生成可视化图表。如果数据有异常值，先问我要怎么处理。"

**该场景触发的全部模块**：
- 前端流式请求与渲染
- 后端API路由 + Service层
- StreamBridge建立SSE流
- Agent工厂创建（make_lead_agent）
- 18个中间件依次执行
- 记忆加载（Memory → DynamicContext）
- LLM调用（带thinking模式）
- 子Agent派发（task工具 → SubagentExecutor）
- 沙箱环境（文件读写、命令执行）
- 工具报错处理（ToolErrorHandlingMiddleware）
- Human-in-the-loop中断（ask_clarification → ClarificationMiddleware）
- 用户确认后恢复执行
- Checkpointer持久化
- LangSmith追踪
- 流式输出回前端

---

## 阶段 ①：前端发起请求

### 1.1 用户输入与乐观更新

**文件**: `frontend/src/core/threads/hooks.ts:428-621`

用户在输入框键入消息后点击发送，触发 `sendMessage()` 回调：

```typescript
// hooks.ts:428
const sendMessage = useCallback(async (threadId, message, extraContext, options) => {
```

**执行步骤**：
1. **防重入检查** `sendInFlightRef.current = true`（hooks.ts:437）
2. **构建乐观消息**：立即显示用户消息，不等服务器响应（hooks.ts:466-485）
   - 如果有文件附件，先显示"uploading"状态
   - `setOptimisticMessages(newOptimistic)` 更新UI
3. **文件上传**（如果有附件）：
   - `uploadFiles(threadId, files)` → `POST /api/threads/{threadId}/uploads`（hooks.ts:517）
   - 后端 `backend/app/gateway/routers/uploads.py` 处理文件上传
   - 自动转换 PDF/PPT/Excel/Word 为文本
   - 返回 `virtual_path`（如 `/mnt/user-data/uploads/data.csv`）

### 1.2 构建请求体并提交

**文件**: `frontend/src/core/threads/hooks.ts:566-612`

```typescript
await thread.submit(
  {
    messages: [{
      type: "human",
      content: [{ type: "text", text: "帮我分析这个CSV数据文件..." }],
      additional_kwargs: { files: [{ path: "/mnt/user-data/uploads/data.csv", ... }] }
    }]
  },
  {
    threadId: threadId,
    streamSubgraphs: true,       // 启用子图事件流
    streamResumable: true,       // 启用SSE可恢复
    config: { recursion_limit: 1000 },
    context: {
      ...context,                // 用户设置：model_name, mode 等
      thinking_enabled: true,    // ultra模式启用thinking
      is_plan_mode: true,        // ultra模式启用plan mode
      subagent_enabled: true,    // ultra模式启用子代理
      reasoning_effort: "high",  // ultra模式高推理
      thread_id: threadId,
    },
  },
);
```

### 1.3 LangGraph SDK 发出HTTP请求

**文件**: `frontend/src/core/api/api-client.ts:34-58`

`getAPIClient()` 返回的 `LangGraphClient` 调用 `client.runs.stream()`：

```
POST /api/langgraph/threads/{threadId}/runs/stream
```

**nginx路由**：`/api/langgraph/*` → Gateway 8001 端口，重写为 `/api/*`

实际到达 Gateway 的请求：
```
POST /api/threads/{threadId}/runs/stream
```

### 1.4 CSRF保护

**文件**: `frontend/src/core/api/api-client.ts:21-32`

每次请求前，`injectCsrfHeader()` 从 cookie 读取 `csrf_token`，注入 `X-CSRF-Token` 头：

```typescript
function injectCsrfHeader(_url: URL, init: RequestInit): RequestInit {
  const token = readCsrfCookie();
  headers.set("X-CSRF-Token", token);
  return { ...init, headers };
}
```

---

## 阶段 ②：后端API路由层

### 2.1 FastAPI中间件栈

**文件**: `backend/app/gateway/app.py`

HTTP请求依次通过三层中间件：

```
请求 → AuthMiddleware → CSRFMiddleware → CORSMiddleware → 路由处理
```

1. **AuthMiddleware** (`app/gateway/auth_middleware.py`)：
   - 验证JWT token
   - 将 `user.id` 注入 `request.state.user`
   - 无认证模式：user_id 默认为 `"default"`

2. **CSRFMiddleware**：
   - 验证 `X-CSRF-Token` 头与 cookie 匹配
   - 仅对状态变更方法（POST/PUT/DELETE）生效

3. **CORSMiddleware**：
   - 同源默认，跨域需配置 `GATEWAY_CORS_ORIGINS`

### 2.2 路由匹配

**文件**: `backend/app/gateway/routers/thread_runs.py:244-277`

```python
@router.post("/{thread_id}/runs/stream")
@require_permission("runs", "create", owner_check=True, require_existing=True)
async def stream_run(thread_id: str, body: RunCreateRequest, request: Request):
```

**请求体验证**（Pydantic模型 `RunCreateRequest`，thread_runs.py:56-103）：
- `assistant_id`: "lead_agent"（默认）
- `input`: `{messages: [{type: "human", content: "..."}]}`
- `context`: `{model_name, thinking_enabled: true, subagent_enabled: true, ...}`
- `stream_mode`: ["values", "messages-tuple"]
- `multitask_strategy`: "reject"
- `on_disconnect`: "cancel"

**权限检查** `@require_permission("runs", "create")`：
- 验证用户是否有权在此线程创建运行
- `owner_check=True` 验证线程所有权

### 2.3 调用 Service 层

**文件**: `backend/app/gateway/routers/thread_runs.py:261-277`

```python
bridge = get_stream_bridge(request)     # 获取 StreamBridge 单例
run_mgr = get_run_manager(request)      # 获取 RunManager 单例
record = await start_run(body, thread_id, request)  # 创建运行记录
```

返回 `StreamingResponse`：
```python
return StreamingResponse(
    sse_consumer(bridge, record, request, run_mgr),  # SSE异步生成器
    media_type="text/event-stream",
    headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",          # 禁用nginx缓冲
        "Content-Location": f"/api/threads/{thread_id}/runs/{record.run_id}",
    },
)
```

---

## 阶段 ③：Service层 — 运行生命周期

### 3.1 start_run() — 创建并启动运行

**文件**: `backend/app/gateway/services.py:310-423`

```python
async def start_run(body, thread_id, request) -> RunRecord:
```

**执行步骤**：

1. **获取基础设施依赖**（services.py:337-339）：
   ```python
   bridge = get_stream_bridge(request)     # StreamBridge 单例
   run_mgr = get_run_manager(request)      # RunManager 单例
   run_ctx = get_run_context(request)      # RunContext（checkpointer + store）
   ```

2. **模型白名单校验**（services.py:343-358）：
   ```python
   model_name = body_context.get("model_name")
   if model_name:
       resolved = app_config.get_model_config(model_name)
       if resolved is None:
           raise HTTPException(400, "Model not in allowlist")
   ```

3. **创建运行记录**（services.py:360-373）：
   ```python
   record = await run_mgr.create_or_reject(
       thread_id, body.assistant_id,
       on_disconnect=DisconnectMode.cancel,
       metadata=body.metadata,
       multitask_strategy="reject",  # 已有运行时拒绝
   )
   ```
   - 如果线程已有活跃运行 → 抛出 `ConflictError` → HTTP 409
   - 创建 `RunRecord`（run_id, thread_id, status=pending, abort_event, task）

4. **Upsert线程元数据**（services.py:377-388）：
   ```python
   existing = await run_ctx.thread_store.get(thread_id)
   if existing is None:
       await run_ctx.thread_store.create(thread_id, ...)
   else:
       await run_ctx.thread_store.update_status(thread_id, "running")
   ```

5. **解析Agent工厂**（services.py:390）：
   ```python
   agent_factory = resolve_agent_factory(body.assistant_id)
   # → 返回 make_lead_agent 函数引用
   ```

6. **构建运行配置**（services.py:391-401）：
   ```python
   config = build_run_config(thread_id, body.config, body.metadata)
   merge_run_context_overrides(config, body.context)
   # → 注入 model_name, thinking_enabled, subagent_enabled 等
   inject_authenticated_user_context(config, request)
   # → 注入 user_id（后台工具需要）
   ```

7. **启动后台任务**（services.py:403-417）：
   ```python
   task = asyncio.create_task(
       run_agent(
           bridge, run_mgr, record, ctx=run_ctx,
           agent_factory=agent_factory,
           graph_input=graph_input,
           config=config,
           stream_modes=["values", "messages-tuple"],
           stream_subgraphs=True,
       )
   )
   record.task = task
   ```

---

## 阶段 ④：StreamBridge — SSE事件桥接

### 4.1 sse_consumer() — 消费事件并生成SSE帧

**文件**: `backend/app/gateway/services.py:426-464`

```python
async def sse_consumer(bridge, record, request, run_mgr):
    async for entry in bridge.subscribe(record.run_id):
        if entry is HEARTBEAT_SENTINEL:
            yield ": heartbeat\n\n"
        elif entry is END_SENTINEL:
            yield format_sse("end", None)
        else:
            yield format_sse(entry.event, entry.data)
```

### 4.2 MemoryStreamBridge — 事件缓冲与分发

**文件**: `backend/packages/harness/deerflow/runtime/stream_bridge/memory.py`

**生产者侧**（run_agent调用）：
```python
await bridge.publish(run_id, "values", serialized_state)
await bridge.publish(run_id, "messages", serialized_chunk)
await bridge.publish_end(run_id)
```

**内部机制**：
1. 每个run_id对应一个 `_RunStream`（事件列表 + asyncio.Condition）
2. `publish()` 追加事件，通知等待的消费者
3. 超过 `queue_maxsize=256` 时丢弃最旧事件
4. 事件ID格式：`{timestamp}-{sequence}`，支持 `Last-Event-ID` 重连

**消费者侧**（sse_consumer）：
1. `subscribe()` 是异步迭代器，等待新事件
2. 15秒无事件时发送心跳
3. 收到 `END_SENTINEL` 时结束迭代
4. 客户端断开时根据策略（cancel/continue）决定是否中止运行

---

## 阶段 ⑤：Agent创建 — make_lead_agent()

### 5.1 run_agent() 启动执行

**文件**: `backend/packages/harness/deerflow/runtime/runs/worker.py:151-443`

```python
async def run_agent(bridge, run_manager, record, *, ctx, agent_factory, ...):
```

**执行步骤**：

1. **初始化RunJournal**（worker.py:209-217）：
   - 创建事件日志，用于持久化消息和token用量
   - 作为 LangChain 回调注入配置

2. **标记状态为running**（worker.py:220）：
   ```python
   await run_manager.set_status(run_id, RunStatus.running)
   ```

3. **捕获pre-run checkpoint**（worker.py:223-238）：
   - 保存当前检查点快照，用于rollback回退
   - 包含：checkpoint, metadata, pending_writes

4. **发布metadata事件**（worker.py:241-248）：
   ```python
   await bridge.publish(run_id, "metadata", {"run_id": ..., "thread_id": ...})
   ```

5. **构建运行时上下文**（worker.py:258-261）：
   ```python
   runtime_ctx = _build_runtime_context(thread_id, run_id, ...)
   runtime = Runtime(context=runtime_ctx, store=store)
   config["configurable"]["__pregel_runtime"] = runtime
   ```

6. **创建Agent实例**（worker.py:268-272）：
   ```python
   agent = agent_factory(config=runnable_config)
   # → 调用 make_lead_agent(config)
   ```

7. **附加checkpointer和store**（worker.py:285-288）：
   ```python
   agent.checkpointer = checkpointer  # SQLite/PostgreSQL/Memory
   agent.store = store
   ```

8. **流式执行Agent**（worker.py:323-349）：
   ```python
   async for chunk in agent.astream(graph_input, config, stream_mode=["values", "messages"]):
       await bridge.publish(run_id, sse_event, serialize(chunk))
   ```

### 5.2 make_lead_agent() — Agent工厂

**文件**: `backend/packages/harness/deerflow/agents/lead_agent/agent.py:401-518`

```python
def make_lead_agent(config: RunnableConfig):
    return _make_lead_agent(config, app_config=get_app_config())
```

**_make_lead_agent()执行流程**：

1. **提取运行时配置**（agent.py:413-424）：
   ```python
   cfg = _get_runtime_config(config)  # 合并 configurable + context
   thinking_enabled = cfg.get("thinking_enabled", True)       # True
   is_plan_mode = cfg.get("is_plan_mode", False)              # True
   subagent_enabled = cfg.get("subagent_enabled", False)      # True
   model_name = _resolve_model_name(cfg.get("model_name"))    # 解析模型
   ```

2. **创建LLM模型**（agent.py:503）：
   ```python
   model = create_chat_model(
       name=model_name,
       thinking_enabled=True,       # 启用thinking模式
       reasoning_effort="high",     # 高推理强度
   )
   ```
   - `create_chat_model()` 通过反射从config.yaml加载模型类
   - 支持 `supports_thinking` → 激活扩展思考
   - 支持 `supports_vision` → 图像理解能力

3. **加载工具列表**（agent.py:500-501）：
   ```python
   tools = get_available_tools(
       model_name=model_name,
       subagent_enabled=True,       # 包含 task 工具
       app_config=resolved_app_config,
   )
   ```
   工具加载顺序：
   1. config.yaml定义的社区工具（`resolve_variable()`反射加载）
   2. MCP工具（懒初始化 + mtime缓存失效）
   3. 内置工具：`present_files`, `ask_clarification`, `view_image`
   4. 子代理工具：`task`（因为 `subagent_enabled=True`）
   5. 自定义Agent工具：`update_agent`（如果有agent_name）

4. **构建中间件链**（agent.py:507-508）：
   ```python
   middlewares = _build_middlewares(config, model_name, agent_name, ...)
   ```

5. **生成系统提示词**（agent.py:509-515）：
   ```python
   system_prompt = apply_prompt_template(
       subagent_enabled=True,
       max_concurrent_subagents=3,
       available_skills=None,       # 加载全部启用技能
   )
   ```
   提示词包含：
   - Agent角色和规则
   - 可用技能列表（从skills/目录扫描）
   - 子代理使用说明
   - 沙箱路径信息

6. **创建Agent图**（agent.py:502-518）：
   ```python
   return create_agent(
       model=model,
       tools=filtered_tools,
       middleware=middlewares,
       system_prompt=system_prompt,
       state_schema=ThreadState,
   )
   ```

---

## 阶段 ⑥：记忆加载 — DynamicContextMiddleware

### 6.1 before_agent — 注入记忆上下文

**文件**: `backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py`

在所有before_agent中间件执行后，`DynamicContextMiddleware` 将记忆注入到第一条HumanMessage中：

**执行流程**：
1. **加载记忆文件**：
   - 路径：`.deer-flow/users/{user_id}/memory.json`（或per-agent路径）
   - 读取 `user` context + `history` context + 前15条 `facts`

2. **构建system-reminder**：
   ```xml
   <system-reminder>
   Current date: 2026-05-27
   <memory>
   <user_context>
   workContext: 用户偏好Python数据分析...
   topOfMind: 正在进行数据清洗项目...
   </user_context>
   <facts>
   - 用户习惯使用pandas进行数据处理
   - 偏好在/mnt/user-data/outputs/目录输出结果
   - ...
   </facts>
   </memory>
   </system-reminder>
   ```

3. **注入到第一条HumanMessage**：
   - 将 `<system-reminder>` 前置到消息内容中
   - **设计原因**：保持系统提示词完全静态，复用前缀缓存（prefix cache）

---

## 阶段 ⑦：中间件链执行 — before_model

### 7.1 SummarizationMiddleware — 上下文压缩

**条件**: `summarization.enabled = True`

如果当前对话接近token上限：
1. 调用独立的summarization模型压缩旧消息
2. 将旧消息替换为一条 `name="summary"` 的HumanMessage
3. 前端通过 `summarizedRef` 隐藏摘要消息

### 7.2 ViewImageMiddleware — 图像注入

**条件**: `model.supports_vision = True`

如果对话中引用了图片（`view_image` 工具已调用）：
1. 读取图片文件
2. 转换为base64
3. 注入到消息列表中

### 7.3 DeferredToolFilterMiddleware — 工具过滤

**条件**: `tool_search.enabled = True`

隐藏标记为 deferred 的工具schema，减少LLM token消耗。

---

## 阶段 ⑧：LLM调用 — wrap_model_call

### 8.1 DanglingToolCallMiddleware — 修补悬挂调用

**文件**: `backend/packages/harness/deerflow/agents/middlewares/dangling_tool_call_middleware.py`

如果上次运行被中断（用户取消），AIMessage中有tool_calls但没有对应的ToolMessage：
1. 为每个悬挂的tool_call生成占位ToolMessage
2. 内容："Tool execution was interrupted"
3. 确保LLM不会因为缺少tool响应而报错

### 8.2 LLMErrorHandlingMiddleware — LLM错误处理

**文件**: `backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py`

如果LLM调用失败（API限流、模型过载等）：
1. 捕获异常
2. 生成友好的错误消息
3. 返回给中间件链继续处理（不中断运行）

### 8.3 实际LLM调用

```
model.bind_tools(tools).invoke(messages)
```

LLM分析用户请求后，返回AIMessage，可能包含 `tool_calls`：

```python
AIMessage(
    content="",
    tool_calls=[
        {"name": "ask_clarification", "args": {
            "question": "发现数据中有异常值（如年龄>200），要怎么处理？",
            "clarification_type": "approach_choice",
            "options": ["直接删除异常行", "用均值填充", "让我确认每一条"]
        }}
    ]
)
```

**或者**，LLM可能先调用 `task` 工具派发子Agent：

```python
AIMessage(
    content="",
    tool_calls=[
        {"name": "task", "args": {
            "subagent_type": "general-purpose",
            "task": "清洗 /mnt/user-data/uploads/data.csv 数据文件...",
            "max_turns": 10
        }}
    ]
)
```

---

## 阶段 ⑨：子Agent派发 — task工具 → SubagentExecutor

### 9.1 task工具调用

**文件**: `backend/packages/harness/deerflow/subagents/` 中的task工具

当LLM调用 `task` 工具时：

1. **SubagentLimitMiddleware**（after_model阶段）检查并发限制：
   - 如果已有3个子Agent在运行 → 截断多余的tool_calls
   - 保留前 `MAX_CONCURRENT_SUBAGENTS=3` 个

2. **SubagentExecutor.execute_async()** 提交后台任务（executor.py:923-996）：
   ```python
   def execute_async(self, task, task_id=None) -> str:
       result = SubagentResult(task_id=task_id, status=SubagentStatus.PENDING)
       _background_tasks[task_id] = result
       _scheduler_pool.submit(run_task)  # 提交到3线程调度池
       return task_id
   ```

### 9.2 子Agent执行流程

**文件**: `backend/packages/harness/deerflow/subagents/executor.py:626-828`

1. **构建初始状态**（executor.py:579-624）：
   ```python
   state, filtered_tools = await self._build_initial_state(task)
   ```
   - 加载技能：`_load_skills()` → 从 `skills/public/` 和 `skills/custom/` 扫描
   - 应用技能工具约束：`_apply_skill_allowed_tools()`
   - 合并系统提示词 + 技能内容为单个SystemMessage
   - 透传父Agent的 `sandbox_state` 和 `thread_data`

2. **创建子Agent实例**（executor.py:457-489）：
   ```python
   agent = self._create_agent(filtered_tools)
   ```
   - 使用 `build_subagent_runtime_middlewares()` 构建子Agent中间件
   - 子Agent不包含：MemoryMiddleware, TitleMiddleware, SubagentLimitMiddleware
   - 子Agent包含：ThreadDataMiddleware, SandboxMiddleware, ToolErrorHandlingMiddleware

3. **流式执行**（executor.py:704）：
   ```python
   async for chunk in agent.astream(state, config=run_config, stream_mode="values"):
       if result.cancel_event.is_set():  # 协作式取消检查
           result.try_set_terminal(SubagentStatus.CANCELLED)
           return result
       # 收集AI消息
   ```

4. **提取最终结果**（executor.py:746-809）：
   - 查找最后一条AIMessage
   - 提取文本内容
   - 设置终态 `COMPLETED`

### 9.3 沙箱环境执行

**文件**: `backend/packages/harness/deerflow/sandbox/middleware.py`

子Agent在执行文件操作时，通过沙箱工具进行：

1. **SandboxMiddleware.before_agent()**（middleware.py:110-137）：
   - 懒加载模式：首次工具调用时获取沙箱
   - 调用 `provider.acquire(thread_id)` → 返回 `sandbox_id`
   - 注入 `state["sandbox"] = {"sandbox_id": "local:{thread_id}"}`

2. **虚拟路径系统**：
   ```
   Agent视角:                    物理路径:
   /mnt/user-data/workspace  → .deer-flow/users/{uid}/threads/{tid}/user-data/workspace
   /mnt/user-data/uploads    → .deer-flow/users/{uid}/threads/{tid}/user-data/uploads
   /mnt/user-data/outputs    → .deer-flow/users/{uid}/threads/{tid}/user-data/outputs
   /mnt/skills               → deer-flow/skills/
   ```

3. **沙箱工具**（`sandbox/tools.py`）：
   - `bash`: 在沙箱中执行命令（路径翻译 + 错误处理）
   - `read_file`: 读取文件（虚拟路径 → 物理路径翻译）
   - `write_file`: 写入文件（创建目录 + 路径翻译）
   - `str_replace`: 字符串替换（用于精确编辑文件）

### 9.4 子Agent事件流

子Agent执行过程中的事件通过SSE推送到前端：

1. **task_started**: 子Agent开始执行
2. **task_running**: 子Agent产生中间结果（AIMessage）
3. **task_completed / task_failed / task_timed_out**: 终态

前端通过 `onCustomEvent` 接收：
```typescript
// hooks.ts:303-317
onCustomEvent(event) {
  if (event.type === "task_running") {
    updateSubtask({ id: event.task_id, latestMessage: event.message });
  }
}
```

---

## 阶段 ⑩：工具报错处理

### 10.1 工具执行异常

假设子Agent在沙箱中执行 `bash` 命令时出错：

```python
# bash工具执行
result = sandbox.execute_command("python analyze.py")
# → 抛出异常: CommandExecutionError("Python module 'scipy' not found")
```

### 10.2 ToolErrorHandlingMiddleware 拦截

**文件**: `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py:39-67`

```python
async def awrap_tool_call(self, request, handler):
    try:
        return await handler(request)
    except GraphBubbleUp:
        raise  # 保留LangGraph控制流信号
    except Exception as exc:
        return self._build_error_message(request, exc)
```

**错误消息格式**：
```python
ToolMessage(
    content="Error: Tool 'bash' failed with CommandExecutionError: Python module 'scipy' not found. Continue with available context, or choose an alternative tool.",
    tool_call_id="call_abc123",
    name="bash",
    status="error",
)
```

**关键设计**：
- `GraphBubbleUp` 不被捕获（LangGraph的中断/恢复信号）
- 错误转为 `ToolMessage`，不中断Agent运行
- LLM看到错误后可以决定重试、换工具、或告知用户

### 10.3 LLMErrorHandlingMiddleware — LLM调用错误

如果LLM API本身失败（限流、超时等）：

1. 捕获异常
2. 生成用户友好的错误消息
3. 通过 `bridge.publish(run_id, "error", {...})` 通知前端
4. 前端显示toast提示

---

## 阶段 ⑪：Human-in-the-loop中断

### 11.1 LLM调用 ask_clarification

当LLM遇到需要用户决策的情况（如数据异常值处理）：

```python
tool_calls = [{
    "name": "ask_clarification",
    "args": {
        "question": "发现数据中有12行异常值（年龄>200），要怎么处理？",
        "clarification_type": "approach_choice",
        "context": "分析 data.csv 时发现异常数据",
        "options": ["直接删除异常行", "用中位数替换", "让我逐条确认"]
    }
}]
```

### 11.2 ClarificationMiddleware 拦截

**文件**: `backend/packages/harness/deerflow/agents/middlewares/clarification_middleware.py:144-183`

ClarificationMiddleware 是**链中最后一个中间件**，拦截 `ask_clarification` 工具调用：

```python
def _handle_clarification(self, request: ToolCallRequest) -> Command:
    args = request.tool_call.get("args", {})

    # 格式化为用户友好消息
    formatted_message = self._format_clarification_message(args)
    # → "🔀 分析 data.csv 时发现异常数据\n发现数据中有12行异常值..."
    # → "  1. 直接删除异常行"
    # → "  2. 用中位数替换"
    # → "  3. 让我逐条确认"

    tool_message = ToolMessage(
        id=f"clarification:{tool_call_id}",  # 确定性ID
        content=formatted_message,
        tool_call_id=tool_call_id,
        name="ask_clarification",
    )

    # 返回 Command 中断执行
    return Command(
        update={"messages": [tool_message]},
        goto=END,  # 跳转到图的结束节点 → 暂停执行
    )
```

### 11.3 中断机制详解

`Command(goto=END)` 触发LangGraph的中断机制：

1. LangGraph将当前状态保存到checkpoint（包含clarification问题）
2. 图执行暂停，`agent.astream()` 停止产出事件
3. `run_agent()` 检测到正常结束 → 设置状态 `success`
4. `bridge.publish_end(run_id)` → SSE流发送 `end` 事件
5. 前端收到结束信号，显示clarification消息

### 11.4 前端显示clarification消息

前端收到 `messages` 事件，包含 `ask_clarification` 的ToolMessage：

```
event: messages
data: [{"type": "tool", "name": "ask_clarification", "content": "🔀 ...\n  1. ..."}]
```

前端渲染：
- 识别 `name === "ask_clarification"` 的消息
- 显示为带选项的卡片
- 用户点击选项或输入回复

### 11.5 用户回复 — 恢复执行

用户输入回复后，前端再次调用 `thread.submit()`：

```typescript
await thread.submit(
  {
    messages: [{
      type: "human",
      content: [{ type: "text", text: "用中位数替换异常值" }],
    }]
  },
  { threadId: threadId, ... }
);
```

这会发起新的 `POST /api/threads/{threadId}/runs/stream` 请求：
1. LangGraph从上一个checkpoint恢复
2. 新的HumanMessage追加到消息列表
3. Agent继续执行，处理用户决策
4. 子Agent根据决策（中位数替换）继续数据分析

---

## 阶段 ⑫：持久化 — Checkpointer

### 12.1 Checkpoint写入

**文件**: `backend/packages/harness/deerflow/runtime/checkpointer/`

每次Agent图节点执行后，LangGraph自动保存checkpoint：

1. **支持的存储后端**：
   - `MemorySaver`：内存（开发用）
   - `SqliteSaver`：SQLite（`.deer-flow/data/deerflow.db`）
   - `PostgresSaver`：PostgreSQL（生产推荐）

2. **Checkpoint内容**：
   ```python
   {
       "channel_values": {
           "messages": [...],          # 完整消息列表
           "title": "数据分析...",      # 自动生成标题
           "artifacts": ["/mnt/user-data/outputs/chart.png"],
           "sandbox": {"sandbox_id": "local:abc123"},
           "thread_data": {
               "workspace_path": ".deer-flow/users/default/threads/xxx/workspace",
               "uploads_path": "...",
               "outputs_path": "..."
           },
           "todos": [...],             # 任务列表
       },
       "channel_versions": {...},
       "versions_seen": {...},
   }
   ```

3. **写入时机**：
   - 每个图节点执行后
   - 中断时（保存当前状态，可恢复）
   - 工具调用后

### 12.2 Checkpoint读取

- **恢复执行**：通过 `checkpoint_id` 从检查点恢复
- **获取最终状态**：`wait_run()` 从检查点读取（thread_runs.py:306-318）
- **标题同步**：`worker.py:422-432` 运行结束后从检查点读取标题

---

## 阶段 ⑬：追踪 — LangSmith Tracing

### 13.1 追踪配置

**文件**: `backend/packages/harness/deerflow/config/app_config.py` (tracing部分)

通过环境变量配置：
```
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=deer-flow
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
```

### 13.2 追踪数据注入

**文件**: `backend/packages/harness/deerflow/agents/lead_agent/agent.py:457-471`

每次运行时注入元数据到LangSmith：

```python
config["metadata"].update({
    "agent_name": "default",
    "model_name": "claude-sonnet-4-6",
    "thinking_enabled": True,
    "reasoning_effort": "high",
    "is_plan_mode": True,
    "subagent_enabled": True,
    "tool_groups": None,
    "available_skills": [...],
})
```

### 13.3 追踪内容

LangSmith记录的完整追踪链：
1. **Run层级**：每次 `run_agent()` 调用
2. **LLM调用**：每次模型调用（输入/输出/token用量）
3. **工具调用**：每次工具执行（参数/结果/耗时）
4. **子Agent**：通过 `tags=["subagent:general-purpose"]` 标记
5. **中间件**：SummarizationMiddleware 标记 `tags=["middleware:summarize"]`

---

## 阶段 ⑭：流式输出回前端

### 14.1 事件序列

完整的SSE事件序列（从 `StreamBridge.publish()` 到前端）：

```
event: metadata
data: {"run_id": "run_abc", "thread_id": "thread_xyz"}

event: values
data: {"title": null, "messages": [...], "artifacts": []}

event: messages
data: [[{"id": "msg_1", "type": "ai", "content": "我来"}], {"metadata": {...}}]
                                                   ↑ delta（增量）

event: messages
data: [[{"id": "msg_1", "type": "ai", "content": "我来分析"}], ...]
                                                   ↑ 同一ID的delta拼接

event: messages
data: [[{"id": "msg_2", "type": "tool", "tool_call_id": "tc_1", "content": "task_id=abc123"}], ...]

event: values
data: {"messages": [...], "todos": [{"content": "数据清洗", "status": "in_progress"}]}

event: messages
data: [[{"id": "msg_3", "type": "tool", "name": "ask_clarification", "content": "🔀 ..."}], ...]

event: end
data: null
```

### 14.2 前端渲染

**文件**: `frontend/src/core/threads/hooks.ts:222-360`

`useStream()` hook 处理各类事件：

1. **onCreated**（hooks.ts:228-236）：
   - 记录 thread_id 和 run_id
   - 更新线程元数据

2. **onLangChainEvent**（hooks.ts:238-244）：
   - 检测 `on_tool_end` 事件
   - 触发 `onToolEnd` 回调（更新工具状态）

3. **onUpdateEvent**（hooks.ts:246-301）：
   - 处理 `SummarizationMiddleware.before_model` 更新
   - 检测标题变化 → 更新 `queryClient` 缓存

4. **onCustomEvent**（hooks.ts:303-331）：
   - `task_running`: 更新子Agent卡片
   - `llm_retry`: 显示重试提示toast

5. **onError**（hooks.ts:332-345）：
   - 清除乐观消息
   - 显示错误toast
   - 刷新token用量

6. **onFinish**（hooks.ts:346-359）：
   - 触发 `onFinish` 回调
   - 刷新线程列表和token用量

### 14.3 消息渲染

**消息合并逻辑**（hooks.ts:630-634）：

```typescript
const mergedMessages = mergeMessages(
  history,              // 历史消息（从API加载）
  thread.messages,      // 当前流式消息
  optimisticMessages,   // 乐观消息（用户输入）
);
```

**去重策略**（hooks.ts:95-126）：
- 按 `messageIdentity`（tool_call_id 或 message.id）去重
- 保留最新版本

**流式渲染**：
- 使用 `Streamdown` 组件实时渲染增量内容
- 同一消息ID的delta自动拼接
- 代码块、表格等自动格式化

---

## 阶段 ⑮：RunJournal — 事件捕获引擎

> RunJournal 是审计追踪的核心，作为 LangChain `BaseCallbackHandler` 挂载到每次运行中，在LLM调用、工具执行、中间件变更的每个关键节点捕获结构化事件，写入 RunEventStore 持久化。

### 15.1 创建与挂载

**文件**: `backend/packages/harness/deerflow/runtime/runs/worker.py:209-217`

RunJournal 在 `run_agent()` 初始化阶段创建：

```python
# worker.py:209
journal = RunJournal(
    run_id=run_id,
    thread_id=thread_id,
    event_store=event_store,  # JSONL 或 DB 后端
    app_config=app_config,
)
# 作为 LangChain 回调注入
config["callbacks"] = [journal]
```

设计要点：
- 每次 `run_agent()` 调用创建独立的 Journal 实例
- 同时作为 `callbacks` 挂载到 LangGraph 和 `RunnableConfig`
- 所有子代理通过 LangChain 回调链自动继承

### 15.2 事件捕获回调

**文件**: `backend/packages/harness/deerflow/runtime/journal.py`

RunJournal 注册了以下 LangChain 回调钩子：

#### on_chat_model_start — LLM调用开始

```python
# journal.py:174
def on_chat_model_start(self, serialized, messages, *, run_id, tags, **kwargs):
```

捕获内容：
- 记录 `llm.request` 事件（仅在首次人类消息时，记录完整输入）
- 调用 `_identify_caller(tags)` 解析调用方标识

#### on_llm_end — LLM调用完成

```python
# journal.py:212
def on_llm_end(self, response, *, run_id, tags, **kwargs):
```

执行逻辑：
1. **去重检查**: `_counted_llm_run_ids` 确保每个 `run_id` 只记录一次
2. **提取用量**: 从 `response.generations[0][0].message.usage_metadata` 提取 `input_tokens`, `output_tokens`, `total_tokens`
3. **累积用量**: 更新 `_total_input_tokens`, `_total_output_tokens`, `_total_tokens`
4. **按调用方分类**: `_identify_caller(tags)` 解析为 `lead_agent` / `subagent:{name}` / `middleware:{name}`
5. **记录事件**: 写入 `llm.ai.response` 事件（包含响应内容 + token用量 + 延迟）
6. **缓冲写入**: 通过 `_flush_async()` 或 `_flush_sync()` 批量持久化到 RunEventStore

#### on_tool_end — 工具执行完成

```python
# journal.py:290
def on_tool_end(self, output, *, run_id, tags, **kwargs):
```

捕获内容：
- 工具名称、输入参数、输出结果
- 特殊处理：如果输出是 `Command` 对象（如 `ClarificationMiddleware` 产生），记录 `goto` 路由信息
- 记录为 `llm.tool.result` 事件

#### on_chat_model_error — LLM调用异常

```python
# journal.py:260
def on_chat_model_error(self, error, *, run_id, **kwargs):
```

记录 `llm.error` 事件，包含异常类型和消息。

### 15.3 调用方识别机制

**文件**: `journal.py:483-500`

```python
def _identify_caller(self, tags: list[str] | None) -> str:
    if not tags:
        return "lead_agent"
    for tag in tags:
        if tag.startswith("subagent:"):
            return tag          # "subagent:bash"
        if tag.startswith("middleware:"):
            return tag          # "middleware:summarize"
    return "lead_agent"
```

标签由 SubagentExecutor 和各中间件通过 `RunnableConfig(tags=[...])` 注入，RunJournal 据此归因每次 LLM 调用的来源。

### 15.4 外部用量合并

**文件**: `journal.py:502-555`

子代理使用独立的 `SubagentTokenCollector` 收集 token 用量，执行完成后通过 RunJournal 合并：

```python
# journal.py:502
def record_external_llm_usage_records(self, records: list[dict], caller: str):
    for record in records:
        # 去重: 检查 source_run_id 是否已在 _counted_external_source_ids
        if record["source_run_id"] in self._counted_external_source_ids:
            continue
        self._counted_external_source_ids.add(record["source_run_id"])
        # 累积到总量
        self._total_input_tokens += record["input_tokens"]
        self._total_output_tokens += record["output_tokens"]
        self._total_tokens += record["total_tokens"]
```

调用时机：`SubagentExecutor._aexecute()` 完成后，将 `SubagentResult.token_usage_records` 传给父代理的 RunJournal。

### 15.5 中间件事件记录

**文件**: `journal.py:557-589`

```python
def record_middleware(self, name: str, state_change: str, data: dict | None = None):
```

中间件通过此方法记录自身状态变更（如 SummarizationMiddleware 压缩了 N 条消息）。事件类型为 `middleware:{name}`。

### 15.6 运行完成数据提取

**文件**: `journal.py:591-608`

```python
def get_completion_data(self) -> dict:
    return {
        "run_id": self.run_id,
        "thread_id": self.thread_id,
        "total_input_tokens": self._total_input_tokens,
        "total_output_tokens": self._total_output_tokens,
        "total_tokens": self._total_tokens,
        "caller_breakdown": dict(self._caller_tokens),  # 按调用方的用量明细
    }
```

在 `run_agent()` 的 `finally` 块中调用，传递给 `RunManager.update_run_completion()` 持久化。

### 15.7 缓冲写入与重试

**文件**: `journal.py:610-609` (底部)

RunJournal 使用缓冲队列减少磁盘/数据库 IO：

```
事件产生 → _buffer (list) → _flush_async() / _flush_sync()
                │
                ▼ (缓冲区满 或 flush触发)
        event_store.put_batch(events)
                │
                ▼ (失败时)
        最多重试 3 次，指数退避
```

---

## 阶段 ⑯：RunEventStore — 事件持久化层

> RunEventStore 是审计事件的持久化抽象层，提供 JSONL 文件和 SQLAlchemy 数据库两种后端实现。所有事件按线程隔离，支持游标分页查询。

### 16.1 抽象接口

**文件**: `backend/packages/harness/deerflow/runtime/events/store/base.py`

```python
class RunEventStore(ABC):
    async def put(self, event: StoredEvent) -> None: ...
    async def put_batch(self, events: list[StoredEvent]) -> None: ...
    async def list_messages(self, thread_id, *, before_seq, after_seq, limit) -> list[StoredEvent]: ...
    async def list_events(self, thread_id, *, run_id, event_type) -> list[StoredEvent]: ...
    async def list_messages_by_run(self, thread_id, run_id) -> list[StoredEvent]: ...
    async def count_messages(self, thread_id) -> int: ...
    async def delete_by_thread(self, thread_id) -> None: ...
    async def delete_by_run(self, thread_id, run_id) -> None: ...
```

事件统一数据结构 `StoredEvent`：
```python
@dataclass
class StoredEvent:
    seq: int              # 单调递增序列号（线程级）
    thread_id: str
    run_id: str
    event_type: str       # "llm.ai.response" | "llm.tool.result" | "llm.error" | ...
    role: str | None      # "human" | "ai" | "tool" | "system"
    data: dict            # 事件载荷
    trace: str | None     # 调用追踪（截断至 10240 字节）
    created_at: datetime
```

### 16.2 JSONL文件后端

**文件**: `backend/packages/harness/deerflow/runtime/events/store/jsonl.py`

**存储路径**: `.deer-flow/threads/{thread_id}/runs/{run_id}.jsonl`

每个事件一行 JSON，追加写入：

```
{"seq": 1, "event_type": "run.start", "role": null, "data": {...}, "ts": "2026-05-27T10:00:00Z"}
{"seq": 2, "event_type": "llm.human.input", "role": "human", "data": {...}, "ts": "..."}
{"seq": 3, "event_type": "llm.ai.response", "role": "ai", "data": {"usage": {...}}, "ts": "..."}
{"seq": 4, "event_type": "llm.tool.result", "role": "tool", "data": {"tool": "bash", "result": "..."}, "ts": "..."}
```

序列号分配：
```python
# jsonl.py:89
def _next_seq(self, thread_id: str) -> int:
    # 线程级单调递增，跨 run_id
    current = self._seq_counter.get(thread_id, 0) + 1
    self._seq_counter[thread_id] = current
    return current
```

分页查询通过 `before_seq` / `after_seq` 实现：读取线程下所有 JSONL 文件，按 seq 排序后截取。

### 16.3 数据库后端

**文件**: `backend/packages/harness/deerflow/runtime/events/store/db.py`

**表结构**:
```sql
CREATE TABLE run_events (
    id        BIGSERIAL PRIMARY KEY,
    seq       BIGINT NOT NULL,
    thread_id VARCHAR NOT NULL,
    run_id    VARCHAR NOT NULL,
    user_id   VARCHAR NOT NULL DEFAULT 'default',
    event_type VARCHAR NOT NULL,
    role      VARCHAR,
    data      JSONB NOT NULL DEFAULT '{}',
    trace     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(thread_id, seq)
);
```

关键实现细节：

1. **序列号分配** (`db.py:131`)：
   ```python
   async def _max_seq_for_thread(self, thread_id) -> int:
       # PostgreSQL: pg_advisory_xact_lock(hash32(thread_id)) 保证并发安全
       # SQLite: SELECT MAX(seq) ... FOR UPDATE
   ```
   使用数据库锁（PostgreSQL advisory lock 或 SQLite FOR UPDATE）确保并发写入时序列号不冲突。

2. **批量写入** (`db.py:194`)：
   ```python
   async def put_batch(self, events):
       async with self._session_factory() as session:
           # 单次锁获取 + 批量 INSERT
           for event in events:
               event.seq = await self._max_seq_for_thread(...)
           session.add_all(events)
   ```

3. **追踪内容截断** (`db.py:68`)：
   ```python
   def _truncate_trace(self, trace: str | None, max_bytes=10240) -> str | None:
       # 防止超长追踪内容撑爆数据库
   ```

4. **用户隔离**: 所有查询自动添加 `WHERE user_id = :uid` 条件。

---

## 阶段 ⑰：安全审计与循环检测

### 17.1 SandboxAuditMiddleware — Bash命令安全审计

**文件**: `backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py`

在 `wrap_tool_call` 阶段拦截 `bash` 工具调用，对命令进行安全分类：

#### 输入验证

```python
# sandbox_audit_middleware.py:280
def _validate_input(self, command: str) -> str | None:
    # 拒绝: 空字符串、超过10000字符、包含null字节
    if not command or len(command) > 10000 or "\x00" in command:
        return "Invalid bash command"
```

#### 两阶段命令分类

```python
# sandbox_audit_middleware.py:163
def _classify_command(self, command: str) -> tuple[RiskLevel, str]:
```

**阶段一：整体匹配** — 对完整命令扫描高风险模式：

| 风险等级 | 模式示例 | 处理 |
|---------|---------|------|
| **高风险** | `rm -rf /`, `dd if=`, `mkfs.`, `\| sh`, `$(...)`, `base64 -d \| sh`, `> /usr/bin/`, `/proc/environ`, `LD_PRELOAD`, `/dev/tcp`, `:(){ :\|:& };:` | **直接阻止** → 返回错误 ToolMessage |

高风险模式共 13 种（`_HIGH_RISK_PATTERNS`，行 25-50）：

```
rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|(/|~))     → rm -rf /
dd\s+if=                                   → 磁盘覆写
mkfs\.                                     → 格式化
\|\s*(ba)?sh\s*                            → 管道到shell
\$\(|`                                     → 命令替换
base64\s+.*\|\s*(ba)?sh                    → base64解码执行
cat\s+.*\|\s*(ba)?sh                       → cat管道执行
>\s*/(usr|bin|sbin|etc|boot|lib)/          → 系统文件覆写
/proc/environ                              → 环境变量泄露
LD_PRELOAD                                 → 动态链接劫持
/dev/tcp                                   → 反弹shell
:()\{.*:\|:&.*\};:                         → fork炸弹
```

**阶段二：逐子命令匹配** — 将命令按 `;`, `&&`, `||`, `|` 拆分，对每个子命令扫描中风险模式：

| 风险等级 | 模式示例 | 处理 |
|---------|---------|------|
| **中风险** | `chmod 777`, `pip install`, `apt install`, `sudo/su`, `PATH=` | **警告追加** → 在工具结果前添加警告文本 |

中风险模式共 5 种（`_MEDIUM_RISK_PATTERNS`，行 53-68）。

#### 审计日志写入

```python
# sandbox_audit_middleware.py:233
def _write_audit(self, state, tool_call_id, command, risk_level, message):
    audit_entry = {
        "type": "sandbox_audit",
        "risk_level": risk_level.value,  # "high" | "medium" | "safe"
        "command": command[:500],         # 截断防止过大
        "message": message,
        "thread_id": state.get("thread_data", {}).get("thread_id"),
        "tool_call_id": tool_call_id,
    }
    # 写入日志 + 发布 custom SSE 事件
```

#### 完整执行流

```
bash工具被调用
    │
    ▼
_validate_input()
    ├─ 无效 → 返回错误ToolMessage
    └─ 有效 ↓
_classify_command()
    ├─ 高风险 → _write_audit(high) → 返回错误ToolMessage（阻止执行）
    ├─ 中风险 → _write_audit(medium) → 追加警告 → 继续执行 → 返回修改后结果
    └─ 安全   → _write_audit(safe) → 正常执行
```

### 17.2 LoopDetectionMiddleware — 循环检测

**文件**: `backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py`

在 `after_model` 阶段检测 LLM 是否陷入工具调用循环：

#### 双层检测机制

**Layer 1 — 哈希集重复检测**（检测完全相同的工具调用集重复）：

```python
# loop_detection_middleware.py:133
def _hash_tool_calls(self, tool_calls: list) -> str:
    # 对每个tool_call生成归一化key，排序后MD5
    keys = sorted(self._stable_tool_key(tc) for tc in tool_calls)
    return hashlib.md5("|".join(keys).encode()).hexdigest()
```

滑动窗口（默认20步）内统计每个哈希值出现次数：
- 出现 ≥ 3 次 → **警告**（追加警告到AIMessage）
- 出现 ≥ 5 次 → **硬停止**（剥离所有 tool_calls + additional_kwargs）

**Layer 2 — 频率检测**（检测单类工具被过度调用）：

```python
# loop_detection_middleware.py:149 (逻辑在 after_model 中)
# 按工具类型统计窗口内调用次数
# 默认阈值: warn=30, hard=50
# 支持按工具类型配置覆盖
```

#### 工具归一化键

```python
# loop_detection_middleware.py:88
def _stable_tool_key(self, tc: dict) -> str:
```

不同工具使用不同归一化策略，避免参数微小变化导致哈希不同：

| 工具 | 归一化策略 |
|------|----------|
| `read_file` | `name + file_path + 行号桶（每100行一个桶）` |
| `write_file` / `str_replace` | `name + file_path + content_hash` |
| `bash` | `name + command_hash` |
| 其他 | `name + salient_fields_hash` |

#### 线程隔离与LRU淘汰

```python
# 每个thread_id独立的检测状态
_thread_states: dict[str, _ThreadLoopState] = {}
# LRU淘汰：最多100个线程
MAX_TRACKED_THREADS = 100
```

线程安全通过 `threading.Lock` 保证。

#### 硬停止行为

```python
# loop_detection_middleware.py (after_model中)
# 检测到硬停止时:
ai_message.tool_calls = []          # 剥离所有工具调用
ai_message.additional_kwargs = {}   # 清空额外元数据
# LLM 将被迫直接回复用户，不再调用工具
```

---

## 阶段 ⑱：Token用量追踪体系

> Token 用量追踪由三层系统组成：RunJournal（主代理级别）、SubagentTokenCollector（子代理级别）、TokenUsageMiddleware（步骤归因层）。三者协作实现完整的 token 审计链。

### 18.1 TokenUsageMiddleware — 步骤归因

**文件**: `backend/packages/harness/deerflow/agents/middlewares/token_usage_middleware.py`

在 `after_model` 阶段，为每次 LLM 调用的 token 用量添加**步骤归因标签**：

#### 步骤类型推断

```python
# token_usage_middleware.py:206
def _infer_step_kind(self, ai_message, prev_tool_results) -> str:
```

根据 AIMessage 的 `tool_calls` 和上下文推断步骤类型：

| 步骤类型 | 判定条件 |
|---------|---------|
| `todo_update` | AIMessage 仅包含 `update_todos` 工具调用 |
| `subagent_dispatch` | AIMessage 包含 `task` 工具调用 |
| `thinking` | AIMessage 有 `thinking` 类型内容块 |
| `final_answer` | AIMessage 无 `tool_calls`（纯文本回复） |
| `tool_batch` | 其他情况（通用工具调用批次） |

#### 工具级归因构建

```python
# token_usage_middleware.py:231
def _build_attribution(self, ai_message, tool_results) -> dict:
```

为每个 tool_call 构建详细的归因描述：

```python
{
    "version": 1,
    "kind": "subagent_dispatch",       # 步骤类型
    "actions": [
        {"tool": "task", "subagent_type": "general-purpose", "task_description": "清洗数据..."}
    ],
    "tool_call_ids": ["call_abc123"],
}
```

特定工具的归因逻辑：
- `update_todos` → `kind: "todo_update"`, 记录操作类型（add/update/remove）
- `task` → `kind: "subagent_dispatch"`, 记录子代理类型和任务描述
- `web_search` → 记录搜索查询
- `present_files` → 记录文件路径
- `ask_clarification` → 记录问题类型

#### 子代理 Token 合并

```python
# token_usage_middleware.py:282
# 当子代理完成后，在主代理的AIMessage中合并子代理的token用量
# 向后搜索: 找到触发该子代理的AIMessage
# 将子代理的 usage_metadata 合并到该AIMessage
```

流程：
1. 子代理完成 → `SubagentResult.token_usage_records` 传回主代理
2. RunJournal 通过 `record_external_llm_usage_records()` 累积总量
3. TokenUsageMiddleware 在下一轮 `after_model` 将子代理用量归因到触发的 AIMessage

### 18.2 SubagentTokenCollector — 子代理级收集

**文件**: `backend/packages/harness/deerflow/subagents/token_collector.py`

每个子代理执行创建独立的 Token 收集器：

```python
# executor.py (SubagentExecutor._aexecute)
collector = SubagentTokenCollector(caller=f"subagent:{agent_name}")
config = RunnableConfig(callbacks=[collector])
# ... 执行子代理 ...
records = collector.snapshot_records()
result.token_usage_records = records
```

收集流程：
1. 作为 `BaseCallbackHandler` 注册到 LangChain 回调链
2. `on_llm_end()` 从 `response.generations[0][0].message.usage_metadata` 提取用量
3. 通过 `run_id` 去重（LangChain 可能触发重复回调）
4. 累积到 `_records` 列表
5. `snapshot_records()` 返回浅拷贝给调用方

记录格式：
```python
{
    "source_run_id": "langchain-run-uuid",  # 去重键
    "caller": "subagent:general-purpose",   # 调用方标识
    "input_tokens": 1500,
    "output_tokens": 800,
    "total_tokens": 2300,
}
```

### 18.3 Token 来源分类统计

**文件**: `backend/packages/harness/deerflow/runtime/journal.py:76-85, 345-351, 540-547`

RunJournal 维护三个独立的分类累积器，将每次 LLM 调用的 token 用量归入对应类别：

```python
# journal.py:82-85 — 初始化
self._lead_agent_tokens = 0     # 主代理直接调用
self._subagent_tokens = 0       # 子代理调用
self._middleware_tokens = 0     # 中间件内部调用（如 SummarizationMiddleware 的压缩模型）
```

#### 分类规则（两处调用点相同）

```python
# journal.py:346-351 (on_llm_end) 和 journal.py:540-547 (record_external_llm_usage_records)
if caller.startswith("subagent:"):
    self._subagent_tokens += total_tk
elif caller.startswith("middleware:"):
    self._middleware_tokens += total_tk
else:
    self._lead_agent_tokens += total_tk
```

**分类依据**来自 `_identify_caller(tags)`，tags 由以下位置注入：

| 来源 | 注入时机 | tag 示例 |
|------|---------|---------|
| 主代理 LangGraph 图 | 默认（无 tag） | → `"lead_agent"` |
| SubagentExecutor | 创建子代理 RunnableConfig | `["subagent:general-purpose"]` |
| SummarizationMiddleware | 调用压缩模型时 | `["middleware:summarize"]` |

#### 三层去重机制

```python
# journal.py:88-90
_counted_llm_run_ids: set[str]          # 直接 LLM 调用去重（on_llm_end 内部）
_counted_external_source_ids: set[str]  # 子代理外部记录去重（record_external_llm_usage_records 内部）
_counted_message_llm_run_ids: set[str]  # 消息摘要去重（避免同一 LLM 响应重复记录消息事件）
```

**去重原因**：LangChain 可能对同一个 LLM 调用触发多次 `on_llm_end`（如重试、子图传播），必须通过 `run_id` 保证同一调用只计数一次。

#### 三条计数路径

```
路径1: 主代理 LLM 调用
  LLM响应 → on_llm_end()
    ├─ run_id ∈ _counted_llm_run_ids? → 跳过
    ├─ 提取 usage_metadata → 累积 _total_tokens
    ├─ _identify_caller(tags) → 归入 lead_agent / subagent / middleware
    └─ 累积到对应分类累积器

路径2: 子代理 LLM 调用
  子代理内 LLM响应 → SubagentTokenCollector.on_llm_end()
    └─ 记录到 _records (run_id 去重)
  子代理完成 → snapshot_records() → 返回给父代理
  父代理 RunJournal.record_external_llm_usage_records(records)
    ├─ source_run_id ∈ _counted_external_source_ids? → 跳过
    ├─ 累积到 _total_tokens
    └─ 按 caller 归入 subagent_tokens

路径3: 中间件 LLM 调用（如 SummarizationMiddleware 压缩模型）
  与路径1相同，但 tags=["middleware:summarize"]
  → 归入 middleware_tokens
```

### 18.4 运行完成 — 审计数据持久化

**文件**: `backend/packages/harness/deerflow/runtime/runs/worker.py:380-432`

在 `run_agent()` 的 `finally` 块中，审计数据被完整持久化：

```python
# worker.py finally 块
finally:
    # 1. Journal 刷盘 — 确保所有缓冲事件写入 RunEventStore
    await journal.flush()

    # 2. 提取完成数据 — 包含总token用量和按调用方明细
    completion_data = journal.get_completion_data()

    # 3. RunManager 持久化 — 写入运行完成记录
    await run_manager.update_run_completion(run_id=run_id, status=..., **completion_data)
```

`get_completion_data()` 返回的完整数据结构：

```python
# journal.py:591-608
{
    "total_input_tokens": 12500,      # 所有 LLM 调用的输入 token 总和
    "total_output_tokens": 4200,      # 所有 LLM 调用的输出 token 总和
    "total_tokens": 16700,            # 输入 + 输出（含 LLM 自报的 total）
    "llm_call_count": 8,              # LLM 调用总次数（去重后）
    "lead_agent_tokens": 8500,        # 主代理直接 LLM 调用
    "subagent_tokens": 7200,          # 子代理 LLM 调用
    "middleware_tokens": 1000,         # 中间件内部 LLM 调用
    "message_count": 15,              # 消息总数
    "last_ai_message": "分析完成...",  # 最后一条 AI 消息（截断 2000 字符）
    "first_human_message": "帮我分析", # 首条用户消息（截断 2000 字符）
}
```

### 18.5 数据库持久化 — RunRow 写入

**文件**: `backend/packages/harness/deerflow/persistence/run/sql.py:246-289`

`RunManager.update_run_completion()` 将 `completion_data` 展开写入数据库 `RunRow`：

```python
# sql.py:246-289
async def update_run_completion(self, run_id, *, status, total_input_tokens, total_output_tokens,
    total_tokens, llm_call_count, lead_agent_tokens, subagent_tokens, middleware_tokens,
    message_count, last_ai_message, first_human_message, error=None):

    values = {
        "status": status,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_tokens,
        "llm_call_count": llm_call_count,
        "lead_agent_tokens": lead_agent_tokens,
        "subagent_tokens": subagent_tokens,
        "middleware_tokens": middleware_tokens,
        "message_count": message_count,
        # last_ai_message / first_human_message 截断至 2000 字符
    }
    await session.execute(update(RunRow).where(RunRow.run_id == run_id).values(**values))
```

**RunRow 数据库字段**（token 相关）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `total_tokens` | BIGINT | 单次运行的总 token 数 |
| `total_input_tokens` | BIGINT | 输入 token 数 |
| `total_output_tokens` | BIGINT | 输出 token 数 |
| `llm_call_count` | INT | LLM 调用次数 |
| `lead_agent_tokens` | BIGINT | 主代理 token 数 |
| `subagent_tokens` | BIGINT | 子代理 token 数 |
| `middleware_tokens` | BIGINT | 中间件 token 数 |
| `model_name` | VARCHAR | 使用的模型名称 |

### 18.6 线程级聚合 — 前端 API

**文件**: `backend/app/gateway/routers/thread_runs.py:642-658`

前端通过 `GET /api/threads/{thread_id}/token-usage` 获取线程级聚合统计：

```python
# thread_runs.py:642
@router.get("/{thread_id}/token-usage", response_model=ThreadTokenUsageResponse)
async def thread_token_usage(thread_id, request):
    run_store = get_run_store(request)
    agg = await run_store.aggregate_tokens_by_thread(thread_id)
    return ThreadTokenUsageResponse(thread_id=thread_id, **agg)
```

**文件**: `backend/packages/harness/deerflow/persistence/run/sql.py:291-344`

`aggregate_tokens_by_thread()` 通过 SQL `GROUP BY` 聚合，避免加载所有运行记录：

```sql
SELECT model_name, COUNT(*) as runs,
       SUM(total_tokens), SUM(total_input_tokens), SUM(total_output_tokens),
       SUM(lead_agent_tokens), SUM(subagent_tokens), SUM(middleware_tokens)
FROM runs
WHERE thread_id = :tid AND status IN ('success', 'error')
GROUP BY model_name
```

**返回数据结构** (`ThreadTokenUsageResponse`)：

```typescript
// frontend/src/core/threads/types.ts:35-47
interface ThreadTokenUsageResponse {
  thread_id: string;
  total_tokens: number;           // 线程所有运行的总 token
  total_input_tokens: number;
  total_output_tokens: number;
  total_runs: number;             // 完成的运行总数
  by_model: Record<string, {      // 按模型分组
    tokens: number;
    runs: number;
  }>;
  by_caller: {                    // 按调用方类型分组
    lead_agent: number;
    subagent: number;
    middleware: number;
  };
}
```

### 18.7 前端渲染 — TokenUsageIndicator

**文件**: `frontend/src/components/workspace/token-usage-indicator.tsx`

前端使用两种数据源计算 token 用量显示：

```
数据源1: 后端聚合 API（线程级，跨所有运行）
  GET /api/threads/{tid}/token-usage
    → ThreadTokenUsageResponse.by_caller
    → selectHeaderTokenUsage({ backendUsage })

数据源2: 消息内联 usage_metadata（单条 AI 消息级）
  AIMessage.usage_metadata = { input_tokens, output_tokens, total_tokens }
    → getUsageMetadata(message)
    → accumulateUsage(messages)  // 按消息 id 去重
```

**合并逻辑**（`usage.ts:86-100`）：
```typescript
// 优先使用后端聚合数据（更准确，包含子代理+中间件用量）
// 如果后端数据不可用，退而使用消息内联的 usage_metadata 累加
// 如果有 pending 消息（流式中的新消息），叠加到后端数据上
```

**Token 用量完整数据流**：

```
LLM API 返回 usage_metadata
    │
    ▼
AIMessage.usage_metadata = { input_tokens: 1500, output_tokens: 800, total_tokens: 2300 }
    │
    ├─ 路径A: 直接 LLM 调用
    │   RunJournal.on_llm_end()
    │     ├─ _total_tokens += 2300
    │     ├─ _identify_caller(tags) → "lead_agent"
    │     └─ _lead_agent_tokens += 2300
    │
    └─ 路径B: 子代理 LLM 调用
        SubagentTokenCollector.on_llm_end()
          └─ _records.append({ source_run_id, caller: "subagent:bash", ... })
        子代理完成 → snapshot_records()
          └─ RunJournal.record_external_llm_usage_records(records)
                ├─ _subagent_tokens += 2300
                └─ _total_tokens += 2300
    │
    ▼ 运行完成
journal.get_completion_data()
    → { total_tokens: 16700, lead_agent_tokens: 8500, subagent_tokens: 7200, ... }
    │
    ▼
RunManager.update_run_completion() → SQL UPDATE runs SET ...
    │
    ▼ 前端请求
GET /api/threads/{tid}/token-usage
    → aggregate_tokens_by_thread() → SQL GROUP BY model_name
    → { total_tokens, by_caller: { lead_agent, subagent, middleware }, by_model: {...} }
    │
    ▼
前端 TokenUsageIndicator 渲染
    ├─ 汇总数字: "16.7K tokens"
    ├─ 明细: input/output/total 分项
    └─ 预设切换: off / summary / per_turn / debug
```

    # 4. 标题同步 — 从检查点读取标题，更新 ThreadStore
    title = await _read_title_from_checkpoint(...)
    if title:
        await thread_store.update_display_name(thread_id, title)

    # 5. 线程状态重置
    await thread_store.update_status(thread_id, "idle")

    # 6. SSE 流结束信号
    await bridge.publish_end(run_id)

    # 7. 资源延迟清理（60秒后清理StreamBridge缓冲区）
    bridge.cleanup(run_id, delay=60)
```

### 18.4 审计追踪全链路汇总

一次完整运行产生的审计数据流：

```
LLM调用 ──callback──▸ RunJournal.on_llm_end()
                        ├─ 记录 llm.ai.response 事件
                        ├─ 累积 _total_tokens
                        └─ _flush_async() → event_store.put_batch()
                                                  │
                                                  ▼
                                          ┌─ JSONL ──▸ .deer-flow/threads/{tid}/runs/{rid}.jsonl
                                          └─ DB ────▸ run_events 表
工具调用 ──callback──▸ RunJournal.on_tool_end()
                        ├─ 记录 llm.tool.result 事件
                        └─ _flush_async() → event_store.put_batch()

中间件 ──主动调用──▸ RunJournal.record_middleware()
                        └─ 记录 middleware:{name} 事件

子代理 ──完成回调──▸ SubagentTokenCollector.snapshot_records()
                        └─ records ──▸ RunJournal.record_external_llm_usage_records()
                                            ├─ 累积到总量
                                            └─ 按 caller 分项统计

Bash审计 ──中间件──▸ SandboxAuditMiddleware._write_audit()
                        └─ 写入审计日志 + SSE custom 事件

循环检测 ──中间件──▸ LoopDetectionMiddleware.after_model()
                        ├─ 滑动窗口哈希匹配
                        ├─ 频率阈值检测
                        └─ 硬停止: 剥离 tool_calls

运行完成 ──finally──▸ journal.flush()
                        ├─ get_completion_data() → {total_tokens, caller_breakdown}
                        └─ run_manager.update_run_completion(completion_data)
```

---

## 完整调用链汇总

```
用户点击发送
  │
  ▼
frontend/src/core/threads/hooks.ts:sendMessage()
  ├─ 构建 optimism消息 → setOptimisticMessages()
  ├─ 上传文件 → POST /api/threads/{id}/uploads
  └─ thread.submit() → LangGraph SDK
       │
       ▼ POST /api/langgraph/threads/{id}/runs/stream
       │ (nginx → :8001/api/threads/{id}/runs/stream)
       ▼
backend/app/gateway/app.py — AuthMiddleware → CSRFMiddleware → CORSMiddleware
       │
       ▼
backend/app/gateway/routers/thread_runs.py:stream_run()
  ├─ @require_permission("runs", "create")
  ├─ start_run(body, thread_id, request)
  │    ├─ 模型白名单校验
  │    ├─ RunManager.create_or_reject()
  │    ├─ ThreadStore.upsert()
  │    ├─ build_run_config()
  │    ├─ merge_run_context_overrides()
  │    ├─ inject_authenticated_user_context()
  │    └─ asyncio.create_task(run_agent(...))
  │         │
  │         ▼
  │    backend/packages/harness/deerflow/runtime/runs/worker.py:run_agent()
  │      ├─ RunManager.set_status(running)
  │      ├─ 捕获pre-run checkpoint
  │      ├─ bridge.publish("metadata", ...)
  │      ├─ _build_runtime_context()
  │      ├─ agent_factory(config)  ──────────────────────┐
  │      ├─ agent.checkpointer = checkpointer             │
  │      ├─ agent.astream(input, config, stream_mode)     │
  │      │    │                                           │
  │      │    ▼                                           │
  │      │  make_lead_agent()  ◀──────────────────────────┘
  │      │    ├─ _get_runtime_config()
  │      │    ├─ _resolve_model_name()
  │      │    ├─ create_chat_model()
  │      │    ├─ get_available_tools()
  │      │    │   ├─ 社区工具 (resolve_variable)
  │      │    │   ├─ MCP工具 (lazy + mtime cache)
  │      │    │   ├─ 内置工具
  │      │    │   └─ task工具 (subagent_enabled)
  │      │    ├─ _build_middlewares() → 18个中间件
  │      │    ├─ apply_prompt_template()
  │      │    └─ create_agent(model, tools, middlewares, prompt)
  │      │         │
  │      │         ▼
  │      │    ┌─ before_agent ─────────────────────────┐
  │      │    │ ThreadDataMiddleware  → 创建线程目录    │
  │      │    │ UploadsMiddleware     → 注入文件元数据  │
  │      │    │ SandboxMiddleware     → 获取沙箱(lazy)  │
  │      │    │ DynamicContextMiddleware → 注入记忆+日期 │
  │      │    └──────────────────────────────────────────┘
  │      │         │
  │      │         ▼
  │      │    ┌─ before_model ────────────────────────┐
  │      │    │ SummarizationMiddleware → 压缩上下文   │
  │      │    │ ViewImageMiddleware → 注入图像数据     │
  │      │    │ DeferredToolFilterMiddleware → 过滤    │
  │      │    └────────────────────────────────────────┘
  │      │         │
  │      │         ▼
  │      │    ┌─ wrap_model_call ─────────────────────┐
  │      │    │ DanglingToolCallMiddleware → 修补      │
  │      │    │ LLMErrorHandlingMiddleware → 错误处理  │
  │      │    └────────────────────────────────────────┘
  │      │         │
  │      │         ▼ LLM调用
  │      │    model.bind_tools(tools).invoke(messages)
  │      │         │
  │      │         ▼ AIMessage (含 tool_calls)
  │      │    ┌─ after_model ─────────────────────────┐
  │      │    │ TokenUsageMiddleware → 步骤归因+用量审计[⑮]        │
  │      │    │ TitleMiddleware → 生成标题             │
  │      │    │ SubagentLimitMiddleware → 截断超限调用  │
  │      │    │ LoopDetectionMiddleware → 双层循环检测[⑧]     │
  │      │    └────────────────────────────────────────┘
  │      │         │
  │      │         ▼ (有 tool_calls)
  │      │    ┌─ wrap_tool_call ──────────────────────┐
  │      │    │ GuardrailMiddleware → 前置授权         │
  │      │    │ SandboxAuditMiddleware → 命令安全审计[⑧]      │
  │      │    │ ToolErrorHandlingMiddleware → 错误捕获 │
  │      │    │ ClarificationMiddleware → 中断拦截     │
  │      │    └────────────────────────────────────────┘
  │      │         │
  │      │         ├── 如果是 task 工具:
  │      │         │   SubagentExecutor.execute_async() [+SubagentTokenCollector ②]
  │      │         │     ├─ _build_initial_state()
  │      │         │     │   ├─ _load_skills()
  │      │         │     │   ├─ _apply_skill_allowed_tools()
  │      │         │     │   └─ 合并system_prompt + skills
  │      │         │     ├─ _create_agent(filtered_tools)
  │      │         │     │   └─ build_subagent_runtime_middlewares()
  │      │         │     ├─ agent.astream() → 沙箱执行
  │      │         │     │   ├─ SandboxMiddleware → acquire
  │      │         │     │   ├─ bash工具 → sandbox.execute_command()
  │      │         │     │   │   └─ 虚拟路径 → 物理路径翻译
  │      │         │     │   ├─ ToolErrorHandlingMiddleware
  │      │         │     │   │   └─ 异常 → ToolMessage(error)
  │      │         │     │   └─ AI消息收集
  │      │         │     └─ try_set_terminal(COMPLETED)
  │      │         │
  │      │         ├── 如果是 ask_clarification:
  │      │         │   ClarificationMiddleware._handle_clarification()
  │      │         │     └─ Command(goto=END) → 中断执行
  │      │         │       ├─ Checkpointer保存状态
  │      │         │       ├─ bridge.publish_end()
  │      │         │       └─ SSE流结束 → 前端显示问题
  │      │         │         └─ 用户回复 → 新的runs/stream请求
  │      │         │            └─ 从checkpoint恢复 → 继续执行
  │      │         │
  │      │         └── 如果是其他工具:
  │      │             直接执行 → ToolMessage → 回到before_model
  │      │
  │      │         ▼ (无 tool_calls，最终响应)
  │      │    ┌─ after_agent ─────────────────────────┐
  │      │    │ MemoryMiddleware → 排队记忆更新        │
  │      │    │   └─ MemoryQueue.add()                 │
  │      │    │       └─ 后台线程: LLM提取上下文/facts │
  │      │    │           → 原子写入 memory.json       │
  │      │    └────────────────────────────────────────┘
  │      │
  │      ▼
  │    StreamBridge.publish("values"/"messages"/"custom")
  │      │
  │      ▼ (并发，另一条路径)
  │    sse_consumer(bridge, record, request, run_mgr)
  │      ├─ bridge.subscribe(run_id)
  │      ├─ format_sse(event, data) → SSE帧
  │      └─ yield SSE帧 → StreamingResponse
  │
  │    finally:
  │      ├─ Journal.flush() → 刷盘审计事件[⑮]
  │      ├─ RunManager.update_run_completion() → 持久化审计数据[⑮]
  │      ├─ ThreadStore.update_display_name() → 同步标题
  │      ├─ ThreadStore.update_status("idle")
  │      ├─ bridge.publish_end(run_id)
  │      └─ bridge.cleanup(run_id, delay=60)
  │
  ▼
Frontend接收SSE事件
  ├─ onCreated → 记录IDs
  ├─ onLangChainEvent → 工具状态更新
  ├─ onUpdateEvent → 标题/摘要更新
  ├─ onCustomEvent → 子Agent进度
  ├─ onError → 错误提示
  ├─ onFinish → 刷新数据
  │
  └─ mergeMessages(history, stream, optimistic)
      → 渲染消息列表
```

---

## 文件索引

| 阶段 | 关键文件 | 核心函数/类 |
|------|---------|------------|
| 前端发起 | `frontend/src/core/threads/hooks.ts` | `useThreadStream()`, `sendMessage()` |
| API客户端 | `frontend/src/core/api/api-client.ts` | `getAPIClient()`, `injectCsrfHeader()` |
| 路由层 | `backend/app/gateway/routers/thread_runs.py` | `stream_run()`, `RunCreateRequest` |
| Service层 | `backend/app/gateway/services.py` | `start_run()`, `sse_consumer()`, `format_sse()` |
| StreamBridge | `backend/packages/harness/deerflow/runtime/stream_bridge/memory.py` | `MemoryStreamBridge`, `publish()`, `subscribe()` |
| 运行执行 | `backend/packages/harness/deerflow/runtime/runs/worker.py` | `run_agent()`, `RunContext` |
| Agent工厂 | `backend/packages/harness/deerflow/agents/lead_agent/agent.py` | `make_lead_agent()`, `_build_middlewares()` |
| 状态模式 | `backend/packages/harness/deerflow/agents/thread_state.py` | `ThreadState`, `merge_artifacts` |
| 中间件构建 | `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py` | `build_lead_runtime_middlewares()`, `ToolErrorHandlingMiddleware` |
| 澄清拦截 | `backend/packages/harness/deerflow/agents/middlewares/clarification_middleware.py` | `ClarificationMiddleware`, `Command(goto=END)` |
| 记忆中间件 | `backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py` | `MemoryMiddleware`, `MemoryQueue` |
| 沙箱中间件 | `backend/packages/harness/deerflow/sandbox/middleware.py` | `SandboxMiddleware`, `SandboxProvider` |
| 子Agent执行 | `backend/packages/harness/deerflow/subagents/executor.py` | `SubagentExecutor`, `execute_async()`, `_aexecute()` |
| 沙箱工具 | `backend/packages/harness/deerflow/sandbox/tools.py` | `bash`, `read_file`, `write_file`, `str_replace` |
| 持久化 | `backend/packages/harness/deerflow/runtime/checkpointer/` | `get_checkpointer()`, `checkpointer_context()` |
| 序列化 | `backend/packages/harness/deerflow/runtime/serialization.py` | `serialize()`, `serialize_messages_tuple()` |
| **审计日志** | `backend/packages/harness/deerflow/runtime/journal.py` | `RunJournal`, `on_llm_end()`, `record_external_llm_usage_records()`, `get_completion_data()` |
| **事件存储(JSONL)** | `backend/packages/harness/deerflow/runtime/events/store/jsonl.py` | `JsonlRunEventStore`, `put_batch()`, `list_messages()` |
| **事件存储(DB)** | `backend/packages/harness/deerflow/runtime/events/store/db.py` | `DbRunEventStore`, `_max_seq_for_thread()` |
| **事件存储接口** | `backend/packages/harness/deerflow/runtime/events/store/base.py` | `RunEventStore` (ABC), `StoredEvent` |
| **沙箱安全审计** | `backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py` | `SandboxAuditMiddleware`, `_classify_command()`, `_HIGH_RISK_PATTERNS` |
| **循环检测** | `backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py` | `LoopDetectionMiddleware`, `_hash_tool_calls()`, `_stable_tool_key()` |
| **Token用量** | `backend/packages/harness/deerflow/agents/middlewares/token_usage_middleware.py` | `TokenUsageMiddleware`, `_build_attribution()`, `_infer_step_kind()` |
| **子代理Token收集** | `backend/packages/harness/deerflow/subagents/token_collector.py` | `SubagentTokenCollector`, `on_llm_end()`, `snapshot_records()` |
| **运行持久化** | `backend/packages/harness/deerflow/persistence/run/sql.py` | `update_run_completion()`, `aggregate_tokens_by_thread()` |
| **Token API** | `backend/app/gateway/routers/thread_runs.py` | `thread_token_usage()` |
| **前端Token类型** | `frontend/src/core/threads/types.ts` | `ThreadTokenUsageResponse` (by_caller, by_model) |
| **前端Token API** | `frontend/src/core/threads/api.ts` | `fetchThreadTokenUsage()` |
| **前端Token工具** | `frontend/src/core/messages/usage.ts` | `accumulateUsage()`, `selectHeaderTokenUsage()`, `formatTokenCount()` |
| **前端Token组件** | `frontend/src/components/workspace/token-usage-indicator.tsx` | `TokenUsageIndicator` |
