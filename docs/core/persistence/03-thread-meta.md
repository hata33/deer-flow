# ThreadMeta — 线程元数据持久化

## 模块路径

`deerflow.persistence.thread_meta`

## 文件结构

```
thread_meta/
├── __init__.py    # 导出 + make_thread_store 工厂函数
├── base.py        # ThreadMetaStore 抽象接口 + InvalidMetadataFilterError
├── model.py       # ThreadMetaRow ORM 模型（threads_meta 表定义）
├── sql.py         # ThreadMetaRepository — SQL 实现
└── memory.py      # MemoryThreadMetaStore — 内存实现（LangGraph BaseStore）
```

## 解决的问题

LangGraph 只管理图执行状态（checkpointer），不知道"这个线程叫什么名字"、"谁在用它"、"它关联了哪个 IM 频道"等应用层信息。ThreadMeta 填补了这个空白，提供：

- 线程标题管理（自动生成、手动修改）
- 用户所有权（多租户隔离）
- 线程状态跟踪（idle / active）
- 自定义元数据搜索（如按 IM 频道、按标签过滤线程）
- 访问权限检查（读取用宽松模式，删除用严格模式）

## 设计模式 — 策略模式

ThreadMeta 是唯一采用策略模式的持久化子模块。因为内存模式没有 SQLAlchemy 引擎，必须使用 LangGraph BaseStore 作为替代存储。

```
ThreadMetaStore (抽象基类)
    │
    ├── ThreadMetaRepository (SQL 实现)
    │   └── 通过 async_sessionmaker + SQLAlchemy 操作数据库
    │
    └── MemoryThreadMetaStore (内存实现)
        └── 通过 LangGraph BaseStore 操作键值存储
```

工厂函数 `make_thread_store(session_factory, store)` 根据可用依赖自动选择实现：
- 有 `session_factory` → `ThreadMetaRepository`
- 只有 `store` → `MemoryThreadMetaStore`
- 都没有 → `ValueError`

## 数据模型 — ThreadMetaRow

### 表名: `threads_meta`

| 字段 | 类型 | 说明 |
|------|------|------|
| `thread_id` | String(64), PK | 线程唯一标识 |
| `assistant_id` | String(128), INDEX, nullable | 关联的助手 ID |
| `user_id` | String(64), INDEX, nullable | 所有者用户 ID |
| `display_name` | String(256), nullable | 显示名称/标题 |
| `status` | String(20), default="idle" | 线程状态 |
| `metadata_json` | JSON, default={} | 自定义元数据 |
| `created_at` | DateTime(tz) | 创建时间 |
| `updated_at` | DateTime(tz) | 更新时间（onupdate 自动维护） |

### 索引

| 列 | 用途 |
|-----|------|
| `assistant_id` | 按助手过滤线程 |
| `user_id` | 按用户过滤线程 |

## 抽象接口 — ThreadMetaStore

定义了 8 个异步方法的标准接口：

| 方法 | 功能 | 所有权检查 |
|------|------|-----------|
| `create()` | 创建线程元数据 | user_id 写入记录 |
| `get()` | 获取单个线程 | 是（非 None 时过滤） |
| `search()` | 搜索线程（支持元数据+状态过滤） | 是 |
| `update_display_name()` | 更新标题 | 是（先验证再更新） |
| `update_status()` | 更新状态 | 是（先验证再更新） |
| `update_metadata()` | 合并更新元数据 | 是（先验证再合并） |
| `check_access()` | 检查访问权限 | 两级（宽松/严格） |
| `delete()` | 删除线程元数据 | 是（先验证再删除） |

### check_access 的两级模式

| 模式 | `require_existing` | 记录不存在时 | 记录存在但不匹配时 | 用途 |
|------|---------------------|-------------|-------------------|------|
| 宽松 | `False`（默认） | 返回 `True` | 返回 `False` | 读取操作（兼容未追踪的遗留线程） |
| 严格 | `True` | 返回 `False` | 返回 `False` | 破坏性操作（DELETE、PATCH） |

宽松模式保持向后兼容：LangGraph 可能有线程但 threads_meta 中没有记录（比如通过 LangGraph Studio 直接创建的线程）。

## SQL 实现 — ThreadMetaRepository

### 核心查询逻辑

#### search() — 搜索线程
```
1. 构建基础 SELECT 查询，按 updated_at DESC 排序
2. 应用用户所有权过滤（resolved_user_id != None 时）
3. 应用状态过滤（可选）
4. 应用元数据过滤（可选，使用 json_match）
   ├── 每个键值对通过 validate_metadata_filter_key/value 验证
   ├── 不安全的键被跳过并记录警告
   └── 所有键都不安全 → 抛出 InvalidMetadataFilterError
5. 应用分页（LIMIT + OFFSET）
```

#### update_metadata() — 合并更新
使用 **read-modify-write** 模式保证一致性：
```
1. 读取当前 metadata_json
2. 在 Python 中浅合并（新值覆盖旧值，旧键保留）
3. 写回数据库
整个操作在单个会话/事务中完成
```

#### _check_ownership() — 内部所有权验证
用于更新和删除操作前，先获取行并比对 `user_id`。`resolved_user_id` 为 `None` 时绕过检查。

## 内存实现 — MemoryThreadMetaStore

### 存储委托

使用 LangGraph `BaseStore` 作为底层存储，命名空间为 `("threads",)`：

```python
THREADS_NS = ("threads",)
# 写入: store.aput(THREADS_NS, thread_id, record)
# 读取: store.aget(THREADS_NS, thread_id)
# 搜索: store.asearch(THREADS_NS, filter=..., limit=..., offset=...)
# 删除: store.adelete(THREADS_NS, thread_id)
```

### 与 SQL 实现的差异

| 方面 | SQL 实现 | Memory 实现 |
|------|----------|-------------|
| 存储引擎 | SQLAlchemy AsyncEngine | LangGraph BaseStore |
| JSON 过滤 | `json_match` 编译扩展 | BaseStore 内置 filter |
| 元数据合并 | read-modify-write + 事务 | 读取 → 修改 → 写回 |
| 时间格式 | datetime 对象 | ISO 字符串（`coerce_iso` 兼容旧格式） |
| 值字段 | 无 | `values`（预留给 LangGraph 状态快照） |

### _get_owned_record() — 内部方法

将"获取 + 验证所有权"的通用逻辑抽取为内部方法，避免在每个公开方法中重复相同的代码。返回可变副本（`dict(item.value)`），避免直接修改 Store 中的数据。

### _item_to_dict() — 格式转换

将 `BaseStore.SearchItem` 转换为与 SQL 实现一致的字典格式。使用 `coerce_iso()` 修复早期 Gateway 版本写入的 unix 时间戳格式（早期使用 `str(time.time())` 而非 ISO 格式）。

## InvalidMetadataFilterError

当 `search()` 中所有元数据过滤键都被安全验证拒绝时抛出。设计意图是：不安全的过滤器静默忽略可能返回意外的大量数据（相当于无过滤），明确报错比静默返回更安全。
