# Agent 请求处理流程

> 来源：`agents/lead_agent/agent.py`、`agents/middlewares/*`

用户消息从进入到回答，经过"工厂构建 → 中间件链 → Agent Loop 多轮循环"三个阶段。

```mermaid
flowchart LR
    REQ["用户消息"] --> FACTORY["工厂构建<br/>make_lead_agent()"]
    FACTORY --> MW["中间件链<br/>17 个中间件"]
    MW --> LOOP["Agent Loop<br/>LLM ↔ 工具 多轮循环"]
    LOOP --> RESP["最终回答"]
```

---

## 第一阶段：工厂构建（make_lead_agent）

每次请求调用，从 `config.configurable` 提取 9 个参数，按优先级解析模型，组装 Agent。

```python
create_agent(
    model       = create_chat_model(name, thinking_enabled, reasoning_effort),
    tools       = get_available_tools(model_name, groups, subagent_enabled),
    middleware  = _build_middlewares(config, model_name, agent_name),
    system_prompt = apply_prompt_template(subagent_enabled, max_concurrent, agent_name),
    state_schema = ThreadState,
)
```

模型解析三级优先级：`请求参数 model_name` → `agent_config.model` → `config.yaml models[0]`。启用 thinking 但模型不支持时自动降级关闭。

---

## 第二阶段：中间件链

Agent Loop 单轮中，中间件的四个执行钩子：

```mermaid
flowchart LR
    BA["before_agent<br/>循环开始前一次"] --> WMC["wrap_model_call<br/>LLM 调用前后"]
    WMC --> LLM["[LLM]"]
    LLM --> AM["after_model<br/>LLM 输出后"]
    AM --> WTC["wrap_tool_call<br/>每个工具调用"]
    WTC --> NEXT["回到 wrap_model_call"]
```

17 个中间件按严格顺序排列，每一层假设前面的层已完成工作：

### Base Runtime 中间件

| # | 中间件 | 钩子 | 作用 |
|---|--------|------|------|
| ① | ThreadDataMiddleware | before_agent | 创建线程目录 |
| ② | UploadsMiddleware | before_agent | 注入上传文件 |
| ③ | SandboxMiddleware | before_agent | 懒初始化沙箱 |
| ④ | DanglingToolCallMiddleware | wrap_model_call | 修补缺失 ToolMessage |
| ⑤ | GuardrailMiddleware | wrap_tool_call | 工具调用授权门控 |
| ⑥ | SandboxAuditMiddleware | wrap_tool_call | bash 命令安全审计 |
| ⑦ | ToolErrorHandlingMiddleware | wrap_tool_call | 工具异常兜底 |

### Lead Agent 专属中间件

| # | 中间件 | 条件 | 钩子 | 作用 |
|---|--------|------|------|------|
| ⑧ | SummarizationMiddleware | enabled | wrap_model_call | 接近上限时摘要旧消息 |
| ⑨ | TodoMiddleware | plan_mode | wrap_model_call | 注入 write_todos 提醒 |
| ⑩ | TokenUsageMiddleware | enabled | after_model | 记录 token 用量 |
| ⑪ | TitleMiddleware | 始终 | after_agent | 首次对话生成标题 |
| ⑫ | MemoryMiddleware | 始终 | after_agent | 排队异步记忆更新 |
| ⑬ | ViewImageMiddleware | supports_vision | wrap_model_call | 注入图片 base64 |
| ⑭ | DeferredToolFilterMiddleware | tool_search | wrap_model_call | 隐藏延迟工具 schema |
| ⑮ | SubagentLimitMiddleware | subagent | after_model | 截断超额 task 调用 |
| ⑯ | LoopDetectionMiddleware | 始终 | after_model | 检测循环并中断 |
| ⑰ | ClarificationMiddleware | 始终（最后） | wrap_tool_call | 拦截澄清请求，goto=END |

---

## 第三阶段：Agent Loop

```mermaid
flowchart TD
    START["before_agent 钩子"] --> WMC["wrap_model_call 钩子"]
    WMC --> LLM["LLM 调用"]
    LLM --> AM["after_model 钩子"]
    AM --> HAS{"有 tool_calls?"}
    HAS -->|"是"| WTC["wrap_tool_call 钩子<br/>逐个执行工具"]
    WTC --> WMC
    HAS -->|"否"| AA["after_agent 钩子"]
    AA --> DONE["循环结束，返回回答"]

    style LLM fill:#9cf,stroke:#333
    style DONE fill:#9f9,stroke:#333
```

`messages` 列表驱动循环：LLM 输出追加到 messages，工具回复追加到 messages，直到 LLM 不再输出 `tool_calls`。

---

## 时序示例："帮我分析并修复这段代码"

```mermaid
sequenceDiagram
    participant U as 用户
    participant MW as 中间件链
    participant LLM as LLM
    participant TOOL as 工具

    U->>MW: "帮我分析并修复代码"
    MW->>MW: before_agent: ThreadData + Uploads
    MW->>LLM: wrap_model_call → LLM
    LLM-->>MW: read_file("main.py")
    MW->>MW: after_model: 检查通过
    MW->>TOOL: wrap_tool_call → read_file
    TOOL-->>MW: 代码内容

    MW->>LLM: 第二轮 → LLM
    LLM-->>MW: str_replace(...)
    MW->>TOOL: wrap_tool_call → str_replace
    TOOL-->>MW: "替换成功"

    MW->>LLM: 第三轮 → LLM
    LLM-->>MW: "已修复 bug..."（无 tool_calls）

    MW->>MW: after_agent: Title + Memory
    MW-->>U: "已修复 bug..."
```

---

## 状态流转（ThreadState）

| 字段 | 类型 | 写入者 | 合并策略 |
|------|------|--------|---------|
| `messages` | `list[BaseMessage]` | LLM + 工具 | append（驱动循环） |
| `sandbox` | `SandboxState` | SandboxMiddleware | 覆盖 |
| `thread_data` | `ThreadDataState` | ThreadDataMiddleware | 覆盖 |
| `title` | `str` | TitleMiddleware | 覆盖 |
| `artifacts` | `list[str]` | present_files 工具 | 去重合并 |
| `todos` | `list` | write_todos 工具 | 覆盖 |
| `uploaded_files` | `list[dict]` | UploadsMiddleware | 覆盖 |
| `viewed_images` | `dict` | ViewImageMiddleware | 字典合并 + 清空 |
