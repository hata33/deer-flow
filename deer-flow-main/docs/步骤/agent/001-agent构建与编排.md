Agent 构建与编排——从 `make_lead_agent()` 到可执行编译图的完整流程

---

## 一、总览：工厂函数的五个组装步骤

`make_lead_agent(config: RunnableConfig)` 是 agent 的核心工厂，接收运行时配置，产出 LangGraph 编译图。组装过程分五步：

```
make_lead_agent(config)
  ├─ 1. 解析运行时参数（从 config.configurable 提取）
  ├─ 2. 三级优先级解析模型名称
  ├─ 3. 验证模型能力（thinking、vision）
  ├─ 4. 注入 LangSmith 追踪元数据
  └─ 5. 调用 create_agent() 传入五个参数
        ├─ model        → create_chat_model()
        ├─ tools        → get_available_tools()
        ├─ middleware    → _build_middlewares()
        ├─ system_prompt → apply_prompt_template()
        └─ state_schema → ThreadState
```

最终 `create_agent()` 返回 `CompiledStateGraph`，可以直接 `astream()` / `invoke()` 执行

---

## 二、模型解析（三级优先级）

**文件**：`agents/lead_agent/agent.py` → `_resolve_model_name()`

```
优先级 1：请求参数 config.configurable.model_name / model
         ↓ 未指定
优先级 2：智能体配置 agents_config 中 agent_config.model
         ↓ 未配置
优先级 3：全局默认 config.yaml 中 models[0].name
```

模型创建通过 `create_chat_model(name, thinking_enabled, reasoning_effort)` 完成，内部根据 config.yaml 中 `models[].provider` 反射创建 LLM 实例

**能力校验**：如果请求了 thinking 但模型不支持，自动降级为非 thinking 模式

---

## 三、工具加载（四层工具源）

**文件**：`tools/tools.py` → `get_available_tools()`

工具从四个来源组装，按顺序合并：

| 层 | 来源 | 加载方式 | 条件 |
|----|------|----------|------|
| 1. 配置工具 | `config.yaml` 的 `tools` 列表 | `resolve_variable("module.path:var")` 反射加载 | 按 `groups` 过滤 |
| 2. 内置工具 | `BUILTIN_TOOLS`（present_file、ask_clarification） | 固定列表 | 始终加载 |
| 2b. 子代理工具 | `task_tool` | 条件注入 | `subagent_enabled=True` |
| 2c. 视觉工具 | `view_image_tool` | 条件注入 | `model.supports_vision=True` |
| 3. MCP 工具 | MCP Server 缓存 | `get_cached_mcp_tools()` | `extensions_config` 有启用的 MCP Server |
| 4. ACP 工具 | Agent Communication Protocol | `build_invoke_acp_agent_tool()` | `acp_config` 有配置 |

**工具搜索（tool_search）**：当 `config.tool_search.enabled=True` 时，MCP 工具不直接暴露给 LLM，而是注册到 `DeferredToolRegistry`，agent 通过 `tool_search` 工具按需查找和加载。这样避免工具 schema 过多占用 context

**安全过滤**：当使用 LocalSandboxProvider 时，自动移除宿主机 bash 工具

---

## 四、中间件链（14 个中间件的有序链）

**文件**：`agents/lead_agent/agent.py` → `_build_middlewares()`

中间件按固定顺序组装，ClarificationMiddleware 始终在最后：

```
 0. ThreadDataMiddleware     → 初始化 thread 工作目录
 1. UploadsMiddleware        → 处理上传文件元数据
 2. SandboxMiddleware        → 沙箱环境管理
 3. DanglingToolCallMiddleware → 修补缺失的 ToolMessage
 4. ToolErrorHandlingMiddleware → 工具异常转 ToolMessage
 5. SummarizationMiddleware  → 上下文超长时自动摘要（可选）
 6. TodoMiddleware           → 计划模式任务管理（可选）
 7. TokenUsageMiddleware     → token 用量追踪（可选）
 8. TitleMiddleware          → 首轮对话自动生成标题
 9. MemoryMiddleware         → 对话后排队更新记忆
10. ViewImageMiddleware      → 注入图片详情（可选，需模型支持视觉）
11. DeferredToolFilterMiddleware → 延迟工具 schema 过滤（可选）
12. SubagentLimitMiddleware  → 限制并发子代理数（可选）
13. LoopDetectionMiddleware  → 检测并打断重复工具调用循环
14. ClarificationMiddleware  → 拦截澄清请求并中断流程（始终最后）
```

**两种构建方式**：

| 入口 | 方式 | 适用场景 |
|------|------|----------|
| `make_lead_agent()` | `_build_middlewares()` 硬编码顺序 | Gateway API 服务端 |
| `create_deerflow_agent()` | `RuntimeFeatures` 声明式 + `@Next/@Prev` 定位 | SDK 编程接口 |

SDK 方式中每个特性接受三种值：`True`（内置默认）、`False`（禁用）、`AgentMiddleware` 实例（自定义替换）

---

## 五、系统提示词（动态模板组装）

**文件**：`agents/lead_agent/prompt.py` → `apply_prompt_template()`

提示词是一个大模板 `SYSTEM_PROMPT_TEMPLATE`，通过 `{placeholder}` 动态填充：

```
SYSTEM_PROMPT_TEMPLATE
  ├─ {agent_name}         → "DeerFlow 2.0" 或自定义智能体名
  ├─ {soul}               → <soul> SOUL.md 人格描述 </soul>
  ├─ {memory_context}     → <memory> 记忆注入 </memory>
  ├─ {skills_section}     → <skill_system> 可用技能列表 </skill_system>
  ├─ {deferred_tools_section} → <available-deferred-tools> 延迟工具名 </available-deferred-tools>
  ├─ {subagent_section}   → <subagent_system> 子代理编排规则 </subagent_system>
  ├─ {acp_section}        → ACP 代理说明（可选）
  └─ + <current_date>     → 当前日期
```

### 5.1 记忆注入

```
_get_memory_context(agent_name)
  ├─ get_memory_config() → 检查 enabled + injection_enabled
  ├─ get_memory_data(agent_name) → 从文件加载 memory.json
  ├─ format_memory_for_injection(data, max_tokens) → 按置信度排序、截断到 token 预算
  └─ 返回 <memory>...</memory> 块
```

记忆注入发生在 **构建提示词时**（agent 创建时），不是运行时。所以记忆更新有延迟：本轮对话的记忆更新在 `MemoryMiddleware.after_agent()` 中排队，下一轮 `make_lead_agent()` 调用时才能读到

### 5.2 技能注入

```
get_skills_prompt_section(available_skills)
  ├─ load_skills(enabled_only=True) → 从 skills 目录加载技能
  ├─ 按 available_skills 过滤（可选白名单）
  └─ 生成 <available_skills> XML 块，每个 skill 包含 name、description、location 路径
```

技能是 **声明式引用**：提示词只告诉 agent 技能文件在哪，agent 通过 `read_file` 工具按需读取技能内容（Progressive Loading 模式）

### 5.3 子代理段落

`_build_subagent_section(max_concurrent)` 根据并发限制动态生成编排规则：
- 硬性约束：每次响应最多 N 个 `task` 调用
- 超量任务必须分批：第一轮 N 个 → 等结果 → 第二批 → ... → 最终综合
- 可用子代理：general-purpose（通用）、bash（命令执行，视沙箱配置）

---

## 六、记忆系统（文件存储 + LLM 更新 + 防抖队列）

### 6.1 存储结构

**文件**：`agents/memory/storage.py` → `FileMemoryStorage`

记忆存储在 `memory.json` 文件中（路径由 `memory_config.storage_path` 或 `paths.memory_file` 决定）：

```json
{
  "version": "1.0",
  "lastUpdated": "2026-05-15T08:00:00Z",
  "user": {
    "workContext":    {"summary": "...", "updatedAt": "..."},
    "personalContext": {"summary": "...", "updatedAt": "..."},
    "topOfMind":      {"summary": "...", "updatedAt": "..."}
  },
  "history": {
    "recentMonths":      {"summary": "...", "updatedAt": "..."},
    "earlierContext":    {"summary": "...", "updatedAt": "..."},
    "longTermBackground": {"summary": "...", "updatedAt": "..."}
  },
  "facts": [
    {"id": "fact_xxxx", "content": "...", "category": "preference|knowledge|context|behavior|goal", "confidence": 0.8, "createdAt": "...", "source": "thread_id"}
  ]
}
```

支持按智能体存储：`agent_name` 不为 None 时，记忆文件路径为 `paths.agent_memory_file(agent_name)`

**缓存策略**：基于文件 mtime 的缓存，文件未修改时直接返回缓存。写入使用原子操作（临时文件 + rename）

### 6.2 更新流程

```
用户对话完成
  │
  ├─ MemoryMiddleware.after_agent()
  │   ├─ 过滤消息（只保留 human + 无 tool_calls 的 ai）
  │   ├─ 去除 <uploaded_files> 块（会话级数据不应持久化）
  │   └─ queue.add(thread_id, messages, agent_name)
  │
  ├─ MemoryUpdateQueue（防抖队列）
  │   ├─ 同 thread 的待处理更新：替换为最新
  │   ├─ 重置 debounce 计时器（默认 30s）
  │   └─ 计时器触发 → 批量处理队列
  │
  └─ MemoryUpdater.update_memory()
      ├─ 加载当前 memory.json
      ├─ 格式化对话文本（截断超过 1000 字的消息）
      ├─ LLM 调用（MEMORY_UPDATE_PROMPT）→ 返回 JSON 更新指令
      ├─ _apply_updates()：按 shouldUpdate 更新各 section
      │   ├─ 新增 facts（去重，按置信度阈值过滤）
      │   ├─ 删除 facts（factsToRemove）
      │   └─ facts 上限：超限时按 confidence 排序截断
      ├─ _strip_upload_mentions_from_memory()：清除上传事件引用
      └─ storage.save() 原子写入文件
```

### 6.3 注入时机

记忆注入发生在 `apply_prompt_template()` → `_get_memory_context()` 中，即 **agent 创建时**。这意味着：
- 本轮对话产生的记忆更新，本轮看不到（因为 MemoryMiddleware 在 agent 执行后才排队）
- 下一轮 `make_lead_agent()` 创建新 agent 时，才能读到上轮更新的记忆

---

## 七、Checkpointer 管理

**Checkpointer 不在 agent 构建流程中管理**，而是在更上层：

```
app 启动 → langgraph_runtime(app) → make_checkpointer() → app.state.checkpointer
                                                      → app.state.store

每次请求 → start_run() → run_agent(worker.py)
                        ├─ Runtime 注入：将 checkpointer + store 塞入 config
                        └─ agent.astream(graph_input, config_with_checkpointer)
```

Agent 本身不持有 checkpointer 引用。LangGraph 的 `create_agent()` 编译图在 `astream()` 时通过 config 中的 `configurable.thread_id` 自动读写检查点。每轮 ReAct 循环的 state 变更都会触发检查点写入

---

## 八、完整组装时序

```
前端 POST /api/threads/{id}/runs/stream
  │
  ├─ start_run()
  │   ├─ RunManager 创建 RunRecord
  │   └─ asyncio.create_task(run_agent(...))
  │
  ├─ run_agent() [worker.py]
  │   ├─ Runtime 注入（thread context + store → config）
  │   └─ agent_factory(config) → make_lead_agent(config)
  │
  └─ make_lead_agent(config)
      ├─ 1. 解析参数：thinking_enabled、model_name、plan_mode、subagent_enabled
      ├─ 2. 解析模型：请求 > agent配置 > 全局默认 → create_chat_model()
      ├─ 3. 加载工具：config.yaml + 内置 + MCP + ACP → get_available_tools()
      ├─ 4. 构建中间件：14 个有序中间件 → _build_middlewares()
      ├─ 5. 组装提示词：role + soul + memory + skills + subagent → apply_prompt_template()
      └─ 6. create_agent(model, tools, middleware, prompt, ThreadState)
           → CompiledStateGraph
```

之后 `agent.astream(graph_input, config)` 启动 ReAct 循环，每步自动写检查点、通过 bridge 推流

---

> 本文档：agent 构建由 `make_lead_agent()` 工厂驱动，五个组装步骤产出 LangGraph 编译图。模型三级优先级解析，工具四层来源合并，14 个中间件有序链，提示词动态填充记忆/技能/子代理段落。记忆系统基于文件存储 + LLM 更新 + 防抖队列，注入在构建时而非运行时。Checkpointer 由 LangGraph 框架通过 config 自动管理，agent 不持有引用

**详细拆分**：

| 文件 | 主题 |
|------|------|
| [002-工厂函数与模型解析.md](002-工厂函数与模型解析.md) | `make_lead_agent()` 五步组装、模型三级优先级解析 |
| [003-工具加载.md](003-工具加载.md) | 四层工具源（config/内置/MCP/ACP）、tool_search 延迟加载 |
| [004-中间件链.md](004-中间件链.md) | 14 个中间件的有序链、两种构建方式（Gateway vs SDK） |
| [005-系统提示词.md](005-系统提示词.md) | 动态模板组装、记忆注入、技能注入、子代理段落 |
| [006-记忆系统.md](006-记忆系统.md) | 文件存储、LLM 更新、防抖队列、注入时机 |
| [007-Checkpointer管理.md](007-Checkpointer管理.md) | 检查点注入时机、LangGraph 自动管理机制 |
| [008-完整组装时序.md](008-完整组装时序.md) | 从 HTTP 请求到编译图产出的端到端调用链 |
| [009-lead-agent与subagent协作.md](009-lead-agent与subagent协作.md) | 编排模式、task 工具、并行控制、双线程池执行 |
