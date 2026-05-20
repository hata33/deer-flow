# 06 - 事件追踪：RunJournal

## 解决的问题

Agent 运行过程中发生了很多事情：LLM 被调用了几次、消耗了多少 Token、工具调用做了什么、中间件修改了什么状态。这些信息对以下场景至关重要：

- **历史回放**：用户刷新页面后，需要看到之前的对话内容
- **成本分析**：运营需要知道每次运行消耗了多少 Token，按模型和调用者分类
- **调试排错**：运行失败时，需要回溯每一步操作来定位问题
- **统计展示**：前端列表页显示消息数、最后 AI 消息摘要等

RunJournal 是连接"Agent 执行过程"和"事件持久化存储"的桥梁。

## 核心机制：LangChain 回调

RunJournal 实现了 LangChain 的 `BaseCallbackHandler` 接口。LangChain/LangGraph 在执行过程中会在特定时机自动回调这个接口的方法：

| 回调方法 | 触发时机 | RunJournal 做了什么 |
|----------|----------|---------------------|
| `on_chain_start` | 整个图开始执行 | 如果是根调用（无 parent），记录 run.start 事件 |
| `on_chain_end` | 整个图执行完成 | 记录 run.end 事件，触发缓冲区刷新 |
| `on_chat_model_start` | LLM 调用开始 | 记录开始时间（计算延迟用），提取第一条 HumanMessage |
| `on_llm_end` | LLM 调用完成 | 记录 AI 回复，提取 Token 使用量，累积统计 |
| `on_tool_end` | 工具调用完成 | 记录工具输出（ToolMessage 或 Command） |

### 为什么用回调而不是自己插桩

Agent 的执行可能涉及多层嵌套：
- 主 Agent 调用 LLM → LLM 决定调用子 Agent → 子 Agent 调用 LLM → 子 Agent 调用工具
- 中间件在 LLM 调用前后执行自定义逻辑

如果自己插桩，需要在每个可能的入口点手动添加追踪代码。遗漏任何一个就无法完整追踪。LangChain 的回调机制是框架级的——无论执行嵌套多深，回调都会被触发。

## 事件分类

RunJournal 将事件分为三类（对应 `run_events` 表的 `category` 字段）：

### message —— 面向用户的消息事件

| event_type | 何时产生 | 内容 |
|------------|----------|------|
| `llm.human.input` | 第一次 LLM 调用时提取的 HumanMessage | 完整的用户输入消息 |
| `llm.ai.response` | 每次 LLM 调用完成 | AI 的回复消息（OpenAI 格式） |
| `llm.tool.result` | 每次工具调用完成 | 工具的输出（ToolMessage 或 Command） |

这些事件用于前端的消息列表展示。用户刷新页面后，从 RunEventStore 查询 message 类别的事件来重建对话。

### trace —— 执行追踪事件

| event_type | 何时产生 | 内容 |
|------------|----------|------|
| `run.start` | 图开始执行 | 链名称、调用者标识 |
| `run.end` | 图执行完成 | 输出数据 |
| `run.error` | 图执行出错 | 错误信息 |
| `llm.error` | LLM 调用出错 | 错误信息 |

这些事件用于调试和监控。通过 trace 事件可以了解运行的完整执行轨迹。

### middleware —— 中间件操作事件

| event_type | 何时产生 | 内容 |
|------------|----------|------|
| `middleware:title` | TitleMiddleware 生成标题 | 新标题内容 |
| `middleware:summarize` | SummarizationMiddleware 压缩上下文 | 压缩前后的消息数 |
| `middleware:guardrail` | GuardrailMiddleware 拒绝工具调用 | 被拒绝的工具和原因 |

这些事件记录了中间件对运行状态的有意义修改。

## Token 使用量追踪

### 累积逻辑

RunJournal 在每次 LLM 调用结束时（`on_llm_end`），从 LangChain 的响应对象中提取 `usage_metadata`，包含 input_tokens、output_tokens、total_tokens。

这些数据在内存中累积，不立即写入数据库（避免频繁的数据库写入影响性能）。运行结束时，`get_completion_data()` 返回汇总数据，由 run_agent 一次性写入 RunStore。

### 按调用者分类

Token 使用量不仅记录总数，还按调用者分类：

| 分类 | 标识 | 来源 |
|------|------|------|
| 主 Agent | `lead_agent` | 默认分类，没有特殊标签的 LLM 调用 |
| 子 Agent | `subagent:{name}` | 子 Agent 执行中的 LLM 调用，通过 tags 注入标识 |
| 中间件 | `middleware:{name}` | 中间件发起的 LLM 调用（如标题生成、摘要），通过 tags 注入标识 |

分类的依据是 LangChain 回调中的 `tags` 参数。中间件和子 Agent 在发起 LLM 调用时，会在 tags 中注入自己的标识。RunJournal 的 `_identify_caller()` 方法从 tags 中提取这些标识。

### 为什么这样分类

不同调用者的 Token 消耗有不同含义：
- **主 Agent**：处理用户请求的核心 Token，是"有用"的消耗
- **子 Agent**：并行任务的 Token，可以评估并行化的成本效益
- **中间件**：辅助功能的 Token（如生成标题），可以评估辅助功能的成本

分类统计让运营者可以精确定位成本来源，做出优化决策。

### 去重机制

LangChain 可能对同一个 LLM 调用多次触发 `on_llm_end`。RunJournal 使用三个去重集合：

| 集合 | 防止重复的场景 |
|------|----------------|
| `_counted_llm_run_ids` | 同一个 LLM 调用的 on_llm_end 被多次触发 |
| `_counted_message_llm_run_ids` | 同一个 LLM 调用的消息摘要被多次记录 |
| `_counted_external_source_ids` | 子 Agent 通过 `record_external_llm_usage_records` 报告的用量被重复计数 |

### 外部用量记录

子 Agent 在独立的后台线程中执行，它们的 LLM 调用不经过主线程的 LangChain 回调链。子 Agent 执行完成后，通过 `record_external_llm_usage_records()` 方法将 Token 使用量报告给 RunJournal。

这个方法接受一个包含 `source_run_id`（唯一标识，用于去重）、`caller`（调用者标签）、Token 数量的记录列表。RunJournal 将这些数据合并到主累积器中。

## 缓冲写入机制

### 为什么不立即写入

LangChain 的回调方法是同步的，而 RunEventStore 的写入是异步的。如果每次回调都立即等待写入完成，会阻塞 Agent 的执行流程，显著增加延迟。

### 缓冲策略

RunJournal 维护一个事件缓冲区（`_buffer`），回调触发时只往缓冲区追加数据。写入在以下时机触发：

| 触发条件 | 行为 |
|----------|------|
| 缓冲区达到阈值（默认 20 条） | 异步批量写入，不阻塞回调 |
| 链执行完成（`on_chain_end`） | 异步批量写入 |
| 运行结束（`journal.flush()`） | 同步等待所有写入完成 |

### 失败处理

如果异步写入失败（如数据库连接问题），失败的事件被放回缓冲区前端，下次刷新时重试。这确保了事件不会因为暂时性错误而丢失。

### 并发写入保护

如果上一次异步刷新还没完成，新的刷新请求会被跳过。这避免了对同一 SQLite 文件的并发写入（SQLite 在写入时会锁定整个文件）。

## 便利字段

RunJournal 在追踪过程中还维护几个"便利字段"，这些字段最终写入 runs 表，避免列表页需要从事件表聚合查询：

| 字段 | 来源 | 用途 |
|------|------|------|
| `first_human_message` | 第一次 LLM 调用时提取的 HumanMessage | 列表页展示用户提问摘要 |
| `last_ai_message` | 主 Agent 最后一次包含文本的 AI 回复 | 列表页展示 AI 回复摘要 |
| `message_count` | 所有被记录的消息数量 | 列表页展示消息总数 |

`last_ai_message` 只记录主 Agent 的回复（caller 为 `lead_agent` 或 None），不包括子 Agent 和中间件的 AI 回复。这是因为列表页需要展示的是"给用户看的最终回复"，而不是内部处理信息。
