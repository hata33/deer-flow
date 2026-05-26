# Persistence 实现分析

> 本文档基于源码逐层拆解实现细节。回答"代码怎么写的、为什么这么写"。

## 分层总览

```
persistence/
├── base.py                 # Base ORM 基类（自动 to_dict）
├── engine.py               # 异步引擎生命周期管理
├── run/model.py, sql.py    # RunRow + RunRepository
├── feedback/model.py, sql.py  # FeedbackRow + FeedbackRepository
├── thread_meta/
│   ├── base.py             # ThreadMetaStore 抽象接口
│   ├── sql.py              # SQL 实现
│   └── memory.py           # 内存实现（BaseStore）
└── user/model.py           # UserRow
```

---

## 1. Run 记录结构与生命周期

### RunRow 表结构

`runs` 表字段分五组：

| 分组 | 字段 | 说明 |
|------|------|------|
| 基本信息 | `run_id` (PK), `thread_id`, `assistant_id`, `user_id`, `status` | 运行标识和状态 |
| 运行参数 | `model_name`, `multitask_strategy`, `metadata_json`, `kwargs_json` | 运行配置快照 |
| 便利字段 | `message_count`, `first_human_message`, `last_ai_message` | 避免列表页 JOIN |
| Token 统计 | `total_input_tokens`, `total_output_tokens`, `llm_call_count` 等 | 按调用方分类 |
| 关联信息 | `follow_up_to_run_id` | 运行链追踪 |

便利字段（如 `first_human_message`）是冗余存储，避免列表页查询时 JOIN run_events 表。由 `RunJournal.get_completion_data()` 运行结束时一次性写入。

### 索引策略

复合索引 `ix_runs_thread_status` 优化"按线程+状态查询"场景。`user_id` 有独立索引用于权限过滤。

### 生命周期

```
pending -> running -> success / error / interrupted
```

`RunManager` 通过 `set_status()` 驱动状态转换，每次变更同步持久化到 RunStore（best-effort）。

---

## 2. RunEvent 追踪

事件通过 `RunJournal` 在内存中累积，运行结束时批量持久化，避免每次 LLM 调用都写数据库。

```
LLM 调用 -> on_llm_end callback -> RunJournal 累积
                                     | (运行结束)
                            journal.flush() -> event_store
                            journal.get_completion_data() -> RunStore
```

Token 用量分三类：`lead_agent_tokens`、`subagent_tokens`、`middleware_tokens`，支持细粒度成本分析。

`RunRepository.aggregate_tokens_by_thread()` 用 SQL `GROUP BY model_name` 聚合已完成运行（status in success/error），在数据库端完成计算避免加载全量数据。

---

## 3. Thread 元数据管理

`ThreadMetaStore` 接口方法：

| 方法 | 功能 |
|------|------|
| `create()` | 创建元数据（含 user_id, display_name, status） |
| `get()` | 获取记录（含所有权验证） |
| `search()` | 按 metadata/status 过滤 |
| `update_display_name()` | 更新标题 |
| `update_status()` | 更新状态（idle/running） |
| `update_metadata()` | 浅合并自定义元数据 |
| `check_access()` | 权限检查（宽松/严格模式） |
| `delete()` | 删除记录 |

`check_access()` 双模式：宽松模式（`require_existing=False`）记录不存在也返回 True，兼容遗留线程；严格模式（`require_existing=True`）必须存在且所有者匹配，用于删除操作。

内存实现 `MemoryThreadMetaStore` 委托给 LangGraph `BaseStore` 的 `("threads",)` 命名空间，通过 `asearch(filter=dict)` 搜索。SQL 实现使用标准 SQLAlchemy 查询构建器。

---

## 4. Feedback CRUD 操作

`FeedbackRow` 唯一约束 `(thread_id, run_id, user_id)` 支持 upsert 语义。评分（+1/-1）在 Repository 层校验，不依赖数据库 CHECK 约束。

`FeedbackRepository.upsert()` 查找 (thread_id, run_id, user_id) 组合的已有记录：存在则更新 rating 和 comment 并重置时间戳；不存在则创建新记录。

`aggregate_by_run()` 用 SQL `CASE` 表达式在数据库端聚合 positive/negative 计数：

```sql
SELECT count(*) AS total,
       coalesce(sum(CASE WHEN rating = 1 THEN 1 ELSE 0 END), 0) AS positive,
       coalesce(sum(CASE WHEN rating = -1 THEN 1 ELSE 0 END), 0) AS negative
FROM feedback WHERE thread_id = ? AND run_id = ?
```

---

## 5. 用户数据隔离

### 引擎初始化

`engine.py` 的 `init_engine()` 根据后端配置：memory 不创建引擎（`get_session_factory()` 返回 None）；sqlite 创建引擎 + WAL 模式 + `synchronous=NORMAL` + `foreign_keys=ON`；postgres 创建引擎 + 连接池 + `pool_pre_ping` + 自动建库。

SQLite WAL 通过 `@event.listens_for` 在每个新连接上执行 PRAGMA，是连接级设置。`expire_on_commit=False` 避免提交后访问属性触发额外 SELECT。

### user_id 渗透路径

```
HTTP 请求 -> authz 中间件 -> user_context contextvar
                                    |
              resolve_user_id(AUTO) <- Repository 方法
                                    |
                         WHERE user_id = ?（行级过滤）
```

每个 Repository 方法默认 `user_id=AUTO`，通过 `resolve_user_id()` 从 contextvar 自动解析。显式 `str` 直接使用，显式 `None` 绕过过滤（迁移/CLI 场景）。
