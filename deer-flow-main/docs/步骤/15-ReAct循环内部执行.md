deer-flow-main\backend\packages\harness\deerflow\agents\middlewares\

承接 007 `create_agent()` 产出的编译图，本步骤展开 `agent.astream()` 内部 ReAct 循环的实际执行流程——中间件如何在各阶段介入、工具调用如何分发、状态如何在 ThreadState 间流转

---

**ReAct 循环结构**

`agent.astream()` 内部是一个 LangGraph 编译图，执行流程：

```
┌─────────────────────────────────────────────────┐
│ ReAct 循环                                       │
│                                                  │
│  1. before_agent（中间件钩子）                      │
│  2. LLM 调用                                      │
│  3. after_model（中间件钩子）                       │
│  4. 解析工具调用                                    │
│  5. wrap_tool_call（中间件钩子，逐个工具）            │
│  6. 工具执行                                       │
│  7. after_tool（中间件钩子）                        │
│  8. 状态合并回 ThreadState                         │
│  9. 判断是否继续循环 → 回到第 2 步                   │
│                                                  │
│ 结束条件：LLM 不再调用工具 / 达到 recursion_limit    │
└─────────────────────────────────────────────────┘
```

---

**中间件钩子阶段**

中间件通过继承 `AgentMiddleware` 实现以下钩子方法：

| 钩子 | 时机 | 典型用途 |
|------|------|----------|
| `before_agent` | 整个 agent 执行前 | 初始化资源（线程目录、沙箱获取） |
| `after_agent` | 整个 agent 执行后 | 释放资源（沙箱释放）、排队副作用 |
| `after_model` | LLM 返回响应后 | 标题生成、记忆排队、子智能体截断 |
| `wrap_tool_call` / `awrap_tool_call` | 每个工具执行前 | 拦截（澄清）、授权（guardrail）、异常包装 |

每个钩子可以返回 state 更新（dict），合并到 ThreadState 中传递给下一个阶段

---

**before_agent 阶段（agent 执行前，只执行一次）**

```
ThreadDataMiddleware → 创建线程隔离目录
  返回: {thread_data: {workspace_path, uploads_path, outputs_path}}

UploadsMiddleware → 追踪新上传文件
  返回: {uploaded_files: [...]}

SandboxMiddleware → 获取沙箱实例
  返回: {sandbox: {sandbox_id: "..."}}

DanglingToolCallMiddleware → 修补缺失的 ToolMessage
  处理: 为无响应的 tool_calls 注入占位 ToolMessage
```

这一步准备执行环境：线程目录、沙箱、上传文件追踪、历史消息修补。返回值合并到 ThreadState，后续所有中间件和工具都能访问

---

**LLM 调用**

LangGraph 将 ThreadState 中的 messages 列表 + system_prompt 发送给 LLM（由 `create_chat_model` 创建的实例）。LLM 返回的 AIMessage 可能包含：
- 纯文本回复（无工具调用，循环结束）
- 工具调用列表（继续循环）

---

**after_model 阶段（LLM 返回后）**

```
TitleMiddleware → 首次对话后生成标题
  条件: 第一轮对话 + 无现有标题
  操作: 调用 LLM 生成标题，写入 state.title

TodoMiddleware → 更新任务状态（plan_mode）
  操作: 处理 write_todos 工具调用的结果

SubagentLimitMiddleware → 截断超额并发子智能体调用
  条件: subagent_enabled + task 工具调用数超过 max_concurrent
  操作: 保留前 N 个 task 调用，其余移除

LoopDetectionMiddleware → 检测重复工具调用循环
  操作: 识别重复模式并打断

ViewImageMiddleware → 注入 base64 图片数据
  条件: 模型支持 vision
  操作: 将图片转为 base64 注入 messages
```

after_model 在 LLM 返回后、工具执行前介入，可以修改 LLM 的响应（如截断工具调用）

---

**wrap_tool_call 阶段（每个工具执行前）**

```
GuardrailMiddleware → 工具调用授权
  条件: guardrails.enabled
  操作: 评估每个工具调用，拒绝的返回 error ToolMessage

ClarificationMiddleware → 拦截澄清请求
  触发: 工具名为 ask_clarification
  操作: 格式化问题 → Command(goto=END) 中断执行，等待用户回复

ToolErrorHandlingMiddleware → 异常包装
  操作: try/catch 包裹工具执行，异常转为 ToolMessage 返回给 LLM
```

wrap_tool_call 是洋葱模型——外层中间件包裹内层，每个中间件可以决定是否放行（调用 handler）或拦截（直接返回 ToolMessage / Command）

---

**工具执行**

通过 wrap_tool_call 链后，实际执行工具函数。工具来源（007 `get_available_tools` 组装的）：

| 工具 | 来源 | 执行环境 |
|------|------|----------|
| bash, ls, read_file, write_file, str_replace | config.yaml sandbox tools | 沙箱内执行（本地或 Docker） |
| present_files, ask_clarification, view_image | 内置工具 | 直接执行 |
| MCP 工具 | MCP servers | 通过 MCP 协议远程调用 |
| task | 子智能体工具 | 后台线程启动子 agent |
| tavily, jina_ai, firecrawl | 社区工具 | HTTP API 调用 |

沙箱工具需要路径翻译（虚拟路径 `/mnt/user-data/` → 物理路径 `.deer-flow/threads/{id}/`）

---

**状态合并回 ThreadState**

工具执行结果（ToolMessage）追加到 messages 列表。中间件返回的 state 更新通过 LangGraph 的 reducer 合并：

- `messages` — 追加（默认 reducer）
- `artifacts` — `merge_artifacts` 去重合并
- `viewed_images` — `merge_viewed_images` 合并，空 dict 清空全部
- 其他字段 — 直接覆盖

---

**after_agent 阶段（agent 执行结束后）**

```
MemoryMiddleware → 排队记忆更新
  操作: 过滤消息（只保留用户消息 + 最终 AI 回复）→ 加入去抖队列

SandboxMiddleware → 释放沙箱
  操作: 归还沙箱资源
```

---

**完整一轮 ReAct 循环示例**

```
用户: "帮我创建一个 hello.py"

第 1 轮:
  before_agent: ThreadData 创建目录, Sandbox 获取沙箱
  LLM 调用: AIMessage(tool_calls=[{name: "write_file", args: {path: "/mnt/user-data/workspace/hello.py", content: "..."}}])
  after_model: TitleMiddleware 生成标题 "创建 hello.py"
  wrap_tool_call: GuardrailMiddleware 放行 → ToolErrorHandlingMiddleware 包裹
  工具执行: write_file 在沙箱中创建文件 → ToolMessage("文件已创建")
  状态合并: messages 追加 ToolMessage

第 2 轮:
  LLM 调用: AIMessage(content="已为您创建 hello.py...")（无工具调用）
  循环结束

after_agent: MemoryMiddleware 排队记忆更新, Sandbox 释放沙箱
```

> 本步骤：ReAct 循环内部按 `before_agent → LLM → after_model → wrap_tool_call → 工具执行 → 状态合并 → 循环/结束 → after_agent` 的流程执行。中间件在四个钩子阶段介入，准备环境、修改响应、拦截工具、处理副作用。工具执行结果合并回 ThreadState，驱动下一轮循环直到 LLM 不再调用工具
