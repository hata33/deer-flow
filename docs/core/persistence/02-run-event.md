# RunEvent — 运行事件持久化

## 模块路径

`deerflow.persistence.models.run_event`

## 解决的问题

Agent 运行过程中产生大量事件——用户消息、AI 回复、工具调用、生命周期变化等。这些事件需要以有序流的形式持久化，用于：

- 消息回放（前端展示对话历史）
- 事件流订阅（SSE 实时推送）
- 调试分析（追踪工具调用链路）
- 分页查询（加载更多消息）

与 `RunRow`（运行汇总信息）不同，`RunEventRow` 记录的是运行过程中的**每一步事件**。

## 数据模型 — RunEventRow

### 表名: `run_events`

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int, PK, autoincrement | 自增主键 |
| `thread_id` | String(64), NOT NULL | 所属线程 ID |
| `run_id` | String(64), NOT NULL | 所属运行 ID |
| `user_id` | String(64), nullable, INDEX | 对话所有者 ID |
| `event_type` | String(32), NOT NULL | 事件类型标识 |
| `category` | String(16), NOT NULL | 事件分类 |
| `content` | Text, default="" | 事件内容文本 |
| `event_metadata` | JSON, default={} | 事件元数据 |
| `seq` | int, NOT NULL | 事件序号（同一线程内递增） |
| `created_at` | DateTime(tz) | 创建时间 |

### 约束和索引

| 名称 | 类型 | 列 | 用途 |
|------|------|-----|------|
| `uq_events_thread_seq` | UNIQUE | `(thread_id, seq)` | 同一线程内事件序号唯一，防止重复写入 |
| `ix_events_thread_cat_seq` | INDEX | `(thread_id, category, seq)` | 按线程+分类+序号查询（如获取某线程所有消息） |
| `ix_events_run` | INDEX | `(thread_id, run_id, seq)` | 按线程+运行+序号查询（如获取某次运行的所有事件） |

### 事件分类体系

`category` 字段将事件分为三大类：

| category | 含义 | 典型 event_type |
|----------|------|-----------------|
| `message` | 对话消息 | `human`（用户输入）、`ai`（AI 回复） |
| `trace` | 追踪信息 | `tool_call`（工具调用）、`tool_result`（工具结果）、`intermediate_step`（中间步骤） |
| `lifecycle` | 生命周期 | `run_started`、`run_completed`、`run_cancelled` |

### `user_id` 可空设计

`user_id` 设为可空是为了**兼容认证功能引入前创建的历史数据**。新写入由认证中间件自动填充。启动时的孤儿迁移脚本会补充历史数据。

### 存储实现

`RunEventRow` 的存储实现不在 `persistence` 包内，而位于 `deerflow.runtime.events.store.db`。这是因为运行事件的写入逻辑与运行时事件系统紧密耦合（事件缓冲、批量写入等），属于运行时关注点而非纯持久化关注点。

ORM 模型放在 `persistence.models` 中是为了确保 `Base.metadata.create_all()` 能发现并创建 `run_events` 表，以及 Alembic 能检测到表结构变更。

### 自增主键 vs UUID

与 `runs`（UUID 主键）、`feedback`（UUID 主键）不同，`run_events` 使用自增整数主键。原因是：
- 事件量远大于其他表，整数主键的插入和索引性能更优
- 事件不需要全局唯一标识（通过 `thread_id + seq` 即可唯一定位）
- 减少存储空间（8 字节 vs 36 字节 UUID 字符串）
