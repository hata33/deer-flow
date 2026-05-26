# Persistence 设计决策

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

## 核心决策清单

| # | 决策 | 解决的问题 | 权衡 |
|---|------|-----------|------|
| 1 | RunStore 抽象接口 + Memory/SQL 双实现 | 内存开发、SQL 生产可切换 | 抽象层增加间接调用 |
| 2 | ThreadMetaStore 抽象 + Memory 包装 BaseStore | 内存模式无 SQL 引擎时的替代方案 | BaseStore 的搜索能力有限 |
| 3 | Feedback 独立实体 + (thread_id, run_id, user_id) 唯一约束 | 用户情感追踪与运行元数据解耦 | 额外表和 JOIN 查询 |
| 4 | user_id 三态语义（AUTO / str / None） | 认证/非认证环境复用同一套代码 | 语义复杂度增加 |
| 5 | 每个 Repository 方法独立短生命周期会话 | 后台 worker 长时间运行不持有连接 | 每次操作都有会话创建开销 |

---

## 决策 1：RunStore 抽象接口 + 可替换后端

### 动机

DeerFlow 支持三种部署模式：纯内存开发（`backend=memory`）、SQLite 单节点、PostgreSQL 生产集群。运行元数据（状态、token 用量等）的存储必须适配所有模式，同时保持 `RunManager` 的调用代码不变。

### 设计选择

`RunStore`（位于 `runtime/runs/store/base.py`）定义了纯异步 ABC 接口，包含 `put`、`get`、`list_by_thread`、`update_status`、`update_run_completion`、`aggregate_tokens_by_thread` 等方法。

两种实现：
- **MemoryRunStore**：`dict[str, dict]` 内存字典，零依赖，开发和测试使用。
- **RunRepository**（`persistence/run/sql.py`）：基于 SQLAlchemy AsyncSession，支持 SQLite 和 PostgreSQL。

### 为什么接口放在 runtime 包而非 persistence 包

`RunManager`（runtime 层）依赖此接口。如果放在 persistence 包，会造成 runtime → persistence 的反向依赖。将抽象接口放在 runtime 层遵循依赖倒置原则：高层模块定义接口，低层模块实现接口。

### 权衡

- `MemoryRunStore` 的 `aggregate_tokens_by_thread()` 在应用层做聚合（遍历字典），而 `RunRepository` 通过 SQL `GROUP BY` 在数据库端聚合。大数据量时性能差异明显。
- 两种实现的输出格式必须保持一致（`_row_to_dict` 负责字段重映射和时间格式化），否则上层代码行为不一致。

---

## 决策 2：Thread 元数据持久化

### 动机

LangGraph 的线程状态由 checkpointer 管理，但不包含业务元数据（如 `display_name`、`status`、`user_id`、自定义 `metadata`）。这些信息需要在进程重启后仍然可用（例如列出用户的会话列表）。

### 设计选择

`ThreadMetaStore` 抽象基类定义了线程元数据的完整 CRUD 接口。两种实现：

| 实现 | 存储后端 | 适用场景 |
|------|---------|---------|
| `MemoryThreadMetaStore` | LangGraph BaseStore `("threads",)` 命名空间 | `backend=memory` |
| `ThreadMetaRepository` | SQLAlchemy (SQLite/PostgreSQL) | 生产部署 |

### 为什么内存模式用 BaseStore 而不是简单字典

在 `backend=memory` 模式下，没有 SQLAlchemy 引擎。但 LangGraph 的 `BaseStore` 在内存模式下始终可用，且提供基本的搜索和过滤功能（`asearch`）。复用 BaseStore 避免引入额外的存储机制。

### 权衡

- BaseStore 的搜索能力有限（仅支持精确匹配过滤），无法支持复杂的排序或分页查询。
- SQL 实现支持完整的 `WHERE`/`ORDER BY`/`LIMIT`/`OFFSET`，未来可以添加全文搜索等高级功能。

---

## 决策 3：Feedback 独立实体

### 动机

用户反馈（点赞/点踩 + 评论）是评估 Agent 表现的核心数据。将反馈与运行元数据分离有以下原因：

1. **生命周期不同**：运行元数据在运行完成后基本不变，反馈可能在运行完成很久后提交或修改。
2. **粒度不同**：反馈可以针对整个运行（`run_id`），也可以针对特定消息（`message_id`），与运行记录的 1:1 关系不匹配。
3. **查询模式不同**：反馈需要按用户聚合统计（如"某用户给多少条回复点了赞"），与运行列表查询完全不同。

### 设计选择

`FeedbackRow` 独立表，包含 `feedback_id`（PK）、`run_id`、`thread_id`、`user_id`、`rating`（+1/-1）、`comment`、`message_id` 等字段。

唯一约束 `(thread_id, run_id, user_id)` 确保每个用户对同一运行只有一条反馈，支持 `upsert` 语义——用户可以修改反馈而无需先删除再创建。

### 权衡

- 查询某次运行的所有反馈需要 `WHERE thread_id=? AND run_id=?`，比嵌套在运行记录内多一次查询。
- 但聚合统计（如 `aggregate_by_run`）可以在数据库端高效完成，不受运行记录大小影响。

---

## 决策 4：user_id 三态语义

### 动机

DeerFlow 在认证模式（有 `user_id`）和非认证模式（无 `user_id`）下都需要运行。如果每个方法单独处理"有/无用户"逻辑，代码会充斥条件分支。

### 设计选择

所有 Repository 方法接受 `user_id: str | None | _AutoSentinel` 参数，具有三种语义：

| 值 | 含义 | 典型场景 |
|----|------|---------|
| `AUTO`（默认） | 从 contextvar 自动解析 | 正常 HTTP 请求 |
| 显式 `str` | 使用提供的值 | 内部调用、测试 |
| 显式 `None` | 绕过所有者过滤 | 迁移脚本、CLI 工具 |

`resolve_user_id()` 函数封装了三态解析逻辑：AUTO → 从 `user_context` contextvar 读取；str → 直接使用；None → 不应用过滤。

### 权衡

- 类型签名中出现了内部类型 `_AutoSentinel`，对调用者暴露了实现细节。
- 但这比在每个方法中重复 `if user_id is not None ...` 更干净，且确保了所有 Repository 的一致行为。

---

## 决策 5：短生命周期会话

### 动机

agent 运行可能在后台持续数分钟。如果在一个长事务中持有数据库连接，会耗尽连接池。

### 设计选择

每个 Repository 方法通过 `async with self._sf() as session` 创建独立会话，方法结束时自动提交或回滚。`RunRepository.update_status()` 使用 `UPDATE ... WHERE` 直接更新，不加载完整 ORM 对象，减少查询次数。

### 权衡

- 每个方法都有会话创建和销毁开销（通常 < 1ms）。
- 但保证了连接不会泄漏，且不同操作之间的事务隔离性清晰。
- `expire_on_commit=False` 设置避免了提交后访问属性时触发额外 SELECT 查询。
