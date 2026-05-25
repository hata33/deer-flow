# Run — 运行元数据持久化

## 模块路径

`deerflow.persistence.run`

## 文件结构

```
run/
├── __init__.py    # 导出 RunRow, RunRepository
├── model.py       # RunRow ORM 模型（runs 表定义）
└── sql.py         # RunRepository（RunStore 接口的 SQL 实现）
```

## 解决的问题

Agent 每次运行需要记录汇总级元数据——用了什么模型、消耗了多少 Token、运行了多长时间、最终状态是什么。这些信息独立于 LangGraph 检查点（图执行状态），是应用层面的数据需求，用于：

- 运行列表页展示（无需扫描事件流）
- Token 成本分析和优化决策
- 运行状态跟踪（pending → running → completed）
- 跨运行的追问链路追踪

## 数据模型 — RunRow

### 表名: `runs`

| 字段分组 | 字段 | 类型 | 说明 |
|----------|------|------|------|
| **基本信息** | `run_id` | String(64), PK | 运行唯一标识 |
| | `thread_id` | String(64), INDEX | 所属线程 |
| | `assistant_id` | String(128), nullable | 关联的助手 ID |
| | `user_id` | String(64), INDEX, nullable | 所有者用户 ID |
| | `status` | String(20) | 运行状态 |
| **运行参数** | `model_name` | String(128), nullable | 使用的模型名称 |
| | `multitask_strategy` | String(20) | 多任务策略 |
| | `metadata_json` | JSON | 运行元数据 |
| | `kwargs_json` | JSON | 运行参数 |
| | `error` | Text, nullable | 错误信息 |
| **便利字段** | `message_count` | int | 消息总数 |
| | `first_human_message` | Text, nullable | 第一条用户消息摘要 |
| | `last_ai_message` | Text, nullable | 最后一条 AI 消息摘要 |
| **Token 统计** | `total_input_tokens` | int | 输入 Token 总数 |
| | `total_output_tokens` | int | 输出 Token 总数 |
| | `total_tokens` | int | Token 总数 |
| | `llm_call_count` | int | LLM 调用次数 |
| | `lead_agent_tokens` | int | 主 Agent 消耗 |
| | `subagent_tokens` | int | 子 Agent 消耗 |
| | `middleware_tokens` | int | 中间件消耗 |
| **关联信息** | `follow_up_to_run_id` | String(64), nullable | 前一次运行 ID（追问链） |
| **时间戳** | `created_at` | DateTime(tz) | 创建时间 |
| | `updated_at` | DateTime(tz) | 更新时间（自动维护） |

### 索引

| 索引名 | 列 | 用途 |
|--------|-----|------|
| `ix_runs_thread_status` | `(thread_id, status)` | 按线程+状态查询 |

### 便利字段设计意图

`message_count`、`first_human_message`、`last_ai_message` 是冗余字段，故意存在 `runs` 表中而非从 `run_events` 表计算。原因是运行列表页是高频访问页面，每次都 JOIN 事件表代价过高。这些字段在运行完成时由 `RunJournal` 一次性写入。

### 状态值

| 状态 | 含义 |
|------|------|
| `pending` | 已创建，等待执行 |
| `running` | 正在执行 |
| `success` | 执行成功 |
| `error` | 执行失败（附带 error 字段） |
| `timeout` | 执行超时 |
| `interrupted` | 被中断（用户取消或 multitask_strategy=interrupt/rollback） |

## Repository — RunRepository

`RunRepository` 实现了 `RunStore` 抽象接口，提供运行元数据的全部 CRUD 操作。

### 核心方法

#### `put()` — 创建运行记录
- 解析 `user_id`（三态语义）
- 规范化 `model_name`（去空白、截断到 128 字符）
- 安全序列化 `metadata` 和 `kwargs`（处理 Pydantic 模型等不可直接 JSON 化的类型）
- 支持 `created_at` 字符串反序列化

#### `get()` — 获取运行记录
- 包含所有者过滤：如果解析到 `user_id`，只返回属于该用户的记录

#### `list_by_thread()` — 按线程列出运行
- 按创建时间降序（最新的在前）
- 支持所有者过滤和分页限制

#### `update_status()` — 更新运行状态
- 使用 UPDATE 语句直接更新，不加载完整行（性能更优）
- 可选附带错误信息

#### `update_model_name()` — 更新模型名称
- 模型名称可能延迟确定（第一次 LLM 调用时才知道实际使用的模型）

#### `update_run_completion()` — 运行完成时批量更新
- 一次性写入所有统计数据（Token 用量、消息计数、首尾消息摘要）
- 这是 `RunJournal` 在内存中累积后的最终落地操作
- 便利字段截断到 2000 字符防止超长

#### `list_pending()` — 获取待处理队列
- 按创建时间升序（最早的先处理）
- 可选 `before` 参数过滤指定时间之前创建的运行
- 用于任务调度器

#### `aggregate_tokens_by_thread()` — 线程级 Token 聚合
- 使用 SQL `GROUP BY` 在数据库端完成聚合
- 只统计已完成运行（status in success, error）
- 返回按模型和调用方类型分组的统计

### 辅助方法

#### `_safe_json()` — 安全 JSON 序列化
处理链：基本类型 → dict/list 递归 → Pydantic model_dump() → Pydantic dict() → json.dumps() → str()

#### `_row_to_dict()` — ORM 到字典转换
重映射 `metadata_json` → `metadata`、`kwargs_json` → `kwargs`，将 datetime 转为 ISO 字符串，确保与内存实现（MemoryRunStore）格式一致。

#### `_normalize_model_name()` — 模型名称规范化
去空白、截断到 128 字符，防止过长导致数据库写入失败。
