# 中间件功能详解

DeerFlow Agent 通过约 18 个中间件构成完整的横切关注点处理链。中间件按固定顺序串联，每个中间件在特定的生命周期钩子中执行。

## 生命周期钩子

```
before_agent → before_model → wrap_model_call → after_model → wrap_tool_call → after_agent
```

所有中间件同时提供同步和异步版本。

## 中间件执行顺序

以下是 Lead Agent 完整中间件链（按追加顺序）：

| 序号 | 中间件 | 钩子 | 说明 |
|------|--------|------|------|
| 0 | ThreadDataMiddleware | `before_agent` | 创建线程目录 |
| 1 | UploadsMiddleware | `before_agent` | 注入上传文件 |
| 2 | SandboxMiddleware | `before_agent` | 配置沙箱环境 |
| 3 | DanglingToolCallMiddleware | `wrap_model_call` | 修补悬挂调用 |
| 4 | GuardrailMiddleware | `wrap_tool_call` | 安全护栏（可选） |
| 5 | ToolErrorHandlingMiddleware | `wrap_tool_call` | 工具异常处理 |
| 6 | LLMErrorHandlingMiddleware | `wrap_model_call` | LLM 错误重试 + 熔断 |
| 7 | SandboxAuditMiddleware | `wrap_tool_call` | Bash 安全审计 |
| 8 | DynamicContextMiddleware | `before_agent` | 记忆/日期动态注入 |
| 9 | SummarizationMiddleware | `before_model` | 对话摘要压缩（可选） |
| 10 | TodoMiddleware | `before_model` / `after_model` / `wrap_model_call` | 任务追踪 |
| 11 | TokenUsageMiddleware | `after_model` | Token 用量统计 |
| 12 | TitleMiddleware | `after_model` | 自动标题生成 |
| 13 | MemoryMiddleware | `after_agent` | 记忆更新排队 |
| 14 | ViewImageMiddleware | `before_model` | 图像内容注入（可选） |
| 15 | DeferredToolFilterMiddleware | `wrap_model_call` / `wrap_tool_call` | 延迟工具过滤（可选） |
| 16 | SubagentLimitMiddleware | `after_model` | 子代理并发限制（可选） |
| 17 | LoopDetectionMiddleware | `after_model` | 循环检测 |
| 18 | ClarificationMiddleware | `wrap_tool_call` | 澄清拦截 |

## 详解

### [0] ThreadDataMiddleware

**功能**：为每个线程创建独立的工作目录结构。

- 目录结构：`{base_dir}/users/{user_id}/threads/{thread_id}/user-data/{workspace,uploads,outputs}`
- `user_id` 通过 `get_effective_user_id()` 解析（无认证模式回退到 `"default"`）
- 支持懒初始化（`lazy_init=True`）

### [1] UploadsMiddleware

**功能**：将上传文件信息注入到 Agent 上下文中。

- 从 `HumanMessage.additional_kwargs.files` 提取新上传文件元数据
- 扫描线程 uploads 目录获取历史文件（排除本次新增）
- 为文档类型文件提取大纲（`extract_outline`），生成行号索引
- 无大纲时提供内容预览（前 5 行）
- 注入格式化的 `<uploaded_files>` 消息块到用户消息前

### [2] SandboxMiddleware

**功能**：管理沙箱执行环境的生命周期。

- 调用 `SandboxProvider.acquire(thread_id)` 获取沙箱实例
- 将 `sandbox_id` 写入 `ThreadState.sandbox`
- 支持 Local（本地文件系统）和 AIO（Docker 隔离）两种提供者

### [3] DanglingToolCallMiddleware

**功能**：修补悬挂的工具调用（用户中断导致工具结果丢失）。

**问题场景**：`AIMessage` 包含 `tool_calls` 但消息历史中没有对应的 `ToolMessage`，LLM 因消息格式不完整报错。

**修补策略**：
- 扫描消息历史，为每个缺少 `ToolMessage` 的 `tool_call` 注入合成错误响应
- 普通悬挂：`"[Tool call was interrupted and did not return a result.]"`
- 无效工具调用：`"[Tool call could not be executed because its arguments were invalid: {error}]"`

**为什么用 `wrap_model_call` 而非 `before_model`**：`wrap_model_call` 可以精确控制修补消息的插入位置（紧跟在对应 `AIMessage` 之后），`before_model` + `add_messages` reducer 只能追加到末尾。

**工具调用来源归一化**：同时检查三个来源：
1. `msg.tool_calls`（结构化字段）
2. `msg.additional_kwargs["tool_calls"]`（原始提供者载荷）
3. `msg.invalid_tool_calls`（格式错误的调用）

### [4] GuardrailMiddleware（可选）

**功能**：工具调用前置授权，在工具执行前评估安全性。

- 基于 `GuardrailProvider` 协议的可插拔实现
- `AllowlistProvider`：白名单机制（零依赖）
- OAP 策略提供者：如 `aport-agent-guardrails`
- 评估每个工具调用，deny 时返回错误 `ToolMessage`
- `fail_closed` 模式：无匹配规则时默认拒绝

### [5] ToolErrorHandlingMiddleware

**功能**：将工具执行异常转换为错误 `ToolMessage`，保证对话流不中断。

- 捕获所有工具异常（`Exception`）
- 异常信息截断到 500 字符
- 保留 `GraphBubbleUp` 信号（LangGraph 中断/暂停/恢复控制流）
- 返回格式：`"Error: Tool '{name}' failed with {exc_type}: {detail}"`

### [6] LLMErrorHandlingMiddleware

**功能**：LLM 调用错误恢复 — 瞬态错误重试 + 熔断器。

**错误分类**：
- `transient`：超时、连接断开、5xx → 可重试
- `busy`：429、rate limit → 可重试（中英文模式匹配）
- `quota`：billing/credit 不足 → 不可重试
- `auth`：认证失败 → 不可重试

**重试策略**：
- 最大 3 次
- 指数退避：1s → 2s → 4s（上限 8s）
- 支持 `Retry-After` / `Retry-After-Ms` 头部解析
- 通过 `stream_writer` 发射 `llm_retry` 事件

**熔断器**：
- 状态机：Closed → Open → Half-Open → Closed
- 连续失败达阈值（`failure_threshold`）后熔断
- 恢复超时后进入 Half-Open，允许一次探测请求
- 探测成功重置为 Closed，失败回到 Open

### [7] SandboxAuditMiddleware

**功能**：Bash 命令安全审计中间件。

**输入验证**：空命令、超过 10,000 字符、null 字节 → 拒绝

**风险评估**：
- **高风险（block）**：`rm -rf /`, `curl|bash`, `dd if=`, `mkfs`, `fork bomb`, `LD_PRELOAD`, `/dev/tcp/` 等 → 阻止执行
- **中风险（warn）**：`pip install`, `chmod 777`, `sudo/su`, `PATH=` 等 → 允许执行 + 追加警告

**命令拆分**：支持复合命令分析（`; && ||`），引号感知（单引号/双引号），未闭合引号 fail-closed。

### [8] DynamicContextMiddleware

**功能**：将记忆和当前日期作为 `<system-reminder>` 注入到 `HumanMessage`。

**设计原理**：系统提示词保持完全静态以最大化前缀缓存命中率，用户相关内容通过此中间件动态注入。

**注入机制**：
- 首轮：注入完整 reminder（记忆 + 日期）到第一条 `HumanMessage`
- 同日：不注入
- 跨日：注入轻量日期更新提醒到当前 `HumanMessage`

**ID 交换技术**：reminder 消息取原消息 ID（触发 `add_messages` 替换），用户内容以 `{id}__user` 派生 ID 追加其后。

**检测方式**：使用 `additional_kwargs.dynamic_context_reminder` 标志（而非内容子串匹配），防止用户消息中恰好包含 `<system-reminder>` 被误判。

### [9] SummarizationMiddleware（可选）

**功能**：对话摘要压缩，在 token 接近上限时自动触发。

**触发条件**：配置驱动（tokens / messages / fraction）

**分区策略**：
1. 标准分区：cutoff 前 → 摘要，cutoff 后 → 保留
2. 技能 bundle 保护：保留最近 N 个技能文件读取的 `ToolMessage`（不超过 token 预算）
3. 动态上下文保护：保留 `DynamicContextMiddleware` 注入的 reminder

**Hook 机制**：`before_summarization` hooks 在摘要删除消息前触发（如 `memory_flush_hook`）

**摘要消息**：使用 `HumanMessage(name="summary")` 标记，前端不展示但模型可见。

### [10] TodoMiddleware

**功能**：任务追踪中间件，扩展 LangChain `TodoListMiddleware`。

**上下文丢失检测**（`before_model`）：
- 当 `write_todos` 工具调用被摘要截断后，注入提醒消息保持模型对任务列表的感知

**防提前退出**（`after_model`）：
- 模型产出最终响应但仍有未完成任务时，通过 `wrap_model_call` 注入完成提醒
- 最大提醒次数 2 次，防止无限循环
- 提醒不持久化为正常消息（避免泄漏到用户可见流）

**完成提醒**（`wrap_model_call`）：
- 通过 `_augment_request` 在模型请求中追加隐藏的 `HumanMessage`
- 不修改持久化状态，仅在模型调用层面注入

### [11] TokenUsageMiddleware

**功能**：Token 用量统计和步骤归属标注。

**Token 统计**：记录每次 LLM 调用的 `input_tokens` / `output_tokens` / `total_tokens`。

**步骤归属**（`token_usage_attribution`）：
- 分析 `AIMessage` 的 `tool_calls`，推断步骤类型
- 步骤类型：`final_answer` / `thinking` / `tool_batch` / `subagent_dispatch` / `todo_update`
- 细粒度动作：`search` / `subagent` / `present_files` / `clarification` / `todo_start` / `todo_complete` 等

**子代理 token 归属**：
- 子代理完成时，其 token 使用量通过 `tool_call_id` 缓存
- `TokenUsageMiddleware` 搜索对应的 `AIMessage`，将子代理 token 合并到派发消息的 `usage_metadata`

### [12] TitleMiddleware

**功能**：自动生成对话标题。

- 在首次完整交换（1 条用户消息 + 1 条 AI 响应）后触发
- 使用 LLM 生成标题（可配置独立模型）
- 同步模式使用本地回退（截取用户消息前 50 字符）
- 异步模式调用 LLM，失败时回退
- 标题限制：`max_words` / `max_chars`
- 剥离 reasoning 模型的 `<think...>` 标签
- 使用 `middleware:title` 标签标记 LLM 调用

### [13] MemoryMiddleware

**功能**：对话结束后排队记忆更新。

**过滤策略**：
- 仅保留 `user` 消息 + `final AI` 响应（忽略中间工具调用）
- 检测纠正信号和强化信号

**排队机制**：
- 捕获 `user_id`（`ContextVar` 在 `Timer` 线程不可用，必须在排队时捕获）
- 30s 防抖，per-thread 去重
- 后台 LLM 提取事实和更新上下文

### [14] ViewImageMiddleware

**功能**：在 LLM 调用前注入图像内容。

- 检测上一轮是否包含 `view_image` 工具调用
- 验证所有 `tool_calls` 都已完成（有对应 `ToolMessage`）
- 从 `ThreadState.viewed_images` 读取图像数据
- 构建多模态 `HumanMessage`（文本 + base64 图像）
- 仅在视觉模型上启用

### [15] DeferredToolFilterMiddleware（可选）

**功能**：从模型绑定中移除延迟工具的 schema。

- `wrap_model_call`：从 `request.tools` 中移除延迟工具，使 `model.bind_tools` 只接收活跃工具
- `wrap_tool_call`：若模型直接调用了未提升的延迟工具，返回错误 `ToolMessage`
- Agent 通过 `tool_search` 工具在运行时发现和提升延迟工具

### [16] SubagentLimitMiddleware（可选）

**功能**：截断超出限制的并行 `task` 工具调用。

- 默认最大并发：3（硬范围 [2, 4]）
- 保留前 N 个 `task` 调用，丢弃多余的
- 丢弃的调用对模型不可见（静默丢弃）

### [17] LoopDetectionMiddleware

**功能**：检测并打断重复的工具调用循环。

**双层检测**：

1. **哈希检测**：对工具调用集合计算哈希，在滑动窗口中跟踪
   - 相同哈希 ≥ 3 次 → 注入警告到 `AIMessage.content`
   - 相同哈希 ≥ 5 次 → 剥离所有 `tool_calls`，强制输出文本

2. **频率检测**：追踪同一工具类型的调用次数（不限参数）
   - ≥ 30 次 → 警告
   - ≥ 50 次 → 强制停止
   - 支持每工具覆盖（`tool_freq_overrides`）

**线程安全**：每线程独立跟踪，LRU 淘汰（默认 100 线程）。

**已知限制**：警告消息追加到 `AIMessage.content` 而非独立 `HumanMessage`，因为 `after_model` 时 `ToolMessage` 尚未执行，插入非工具消息会破坏配对验证。

### [18] ClarificationMiddleware

**功能**：拦截 `ask_clarification` 工具调用并中断执行。

**流程**：
1. 检测到 `ask_clarification` 工具调用
2. 提取澄清问题及元数据（类型、上下文、选项）
3. 格式化为用户友好消息（带类型图标）
4. 返回 `Command(goto=END)` 中断图执行
5. 用户回复后 LangGraph 自动恢复

**消息类型图标**：
- `missing_info` → ❓
- `ambiguous_requirement` → 🤔
- `approach_choice` → 🔀
- `risk_confirmation` → ⚠️
- `suggestion` → 💡

**兼容性**：处理某些模型（如 Qwen3-Max）将数组参数序列化为 JSON 字符串的情况。

**始终位于链尾**：确保澄清请求在所有其他处理完成后才被拦截。
