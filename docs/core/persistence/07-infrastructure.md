# Infrastructure — 基础设施层

本文档描述持久化层的基础设施组件：引擎管理、ORM 基类、JSON 兼容层和迁移环境。

## 1. 引擎管理 — engine.py

### 职责

管理全局数据库引擎的生命周期：创建、配置、自动建表、关闭。

### 全局单例

```python
_engine: AsyncEngine | None = None           # 全局引擎实例
_session_factory: async_sessionmaker | None = None  # 全局会话工厂
```

整个进程共享同一个引擎实例。模块级变量，不使用类封装。

### 核心函数

#### `init_engine(backend, url, echo, pool_size, sqlite_dir)`

引擎初始化的核心入口。

**三种后端的配置策略**：

| 后端 | 引擎配置 | 特殊处理 |
|------|----------|----------|
| `memory` | 不创建引擎 | `session_factory = None`，各 Repository 需检查 None 并回退 |
| `sqlite` | `create_async_engine(url)` | WAL 模式事件监听器 + 目录自动创建 |
| `postgres` | `create_async_engine(url, pool_size, pool_pre_ping)` | asyncpg 驱动检查 + 自动建库 |

**SQLite WAL 模式配置**（每个新连接上执行）：
```sql
PRAGMA journal_mode=WAL;      -- 写前日志，允许并发读写
PRAGMA synchronous=NORMAL;    -- 只在 WAL 检查点 fsync，平衡安全和性能
PRAGMA foreign_keys=ON;       -- 启用外键约束（SQLite 默认关闭）
```

为什么在每个连接上执行：SQLite PRAGMA 是连接级别的设置，不能全局设置一次。

**PostgreSQL 自动建库**：
当 `create_all` 报 "database does not exist" 时：
1. `_auto_create_postgres_db()` — 连接到 `postgres` 维护库，`CREATE DATABASE`
2. 释放旧引擎
3. 重新创建引擎 + session_factory
4. 再次尝试建表

`CREATE DATABASE` 不能在事务中执行，因此使用 `AUTOCOMMIT` 隔离级别。

#### `init_engine_from_config(config)`

从 `DatabaseConfig` 对象便捷初始化，将配置字段映射到 `init_engine` 参数。

#### `get_session_factory()` → `async_sessionmaker | None`

返回会话工厂。`memory` 模式返回 `None`，各 Repository 必须检查并回退。

#### `get_engine()` → `AsyncEngine | None`

返回当前引擎实例。

#### `close_engine()`

关闭引擎，释放连接池。`Gateway` 关闭时调用。

### JSON 序列化器

```python
def _json_serializer(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
```

`ensure_ascii=False` 确保 JSON 列中的中文字符不被转义为 `\uXXXX`，保持可读性。

### 自动建表

`init_engine` 最后一步调用 `Base.metadata.create_all()` 自动建表。这是**开发便利功能**，生产环境应使用 Alembic 迁移。

流程：
1. 导入 `deerflow.persistence.models` 注册所有 ORM 模型
2. `Base.metadata.create_all()` 创建未存在的表
3. PostgreSQL 特殊处理：如果数据库不存在，先创建数据库再重试

## 2. ORM 基类 — base.py

### 职责

提供所有 DeerFlow ORM 模型的公共基类，包含通用序列化和调试方法。

### `Base(DeclarativeBase)`

#### `to_dict(exclude=None)` — 自动序列化

通过 SQLAlchemy 的 `inspect()` 遍历所有映射列属性，自动生成字典。

优点：
- 不需要每个模型手动列举字段
- 新增字段时自动包含，无需维护序列化代码

`exclude` 参数用于排除敏感或内部字段。

#### `__repr__()` — 调试输出

格式：`ClassName(col1=val1, col2=val2, ...)`，便于日志和调试。

### 独立性

DeerFlow 的 `Base` 与 LangGraph 检查点的表完全独立。检查点有自己的元数据和迁移生命周期。

## 3. JSON 兼容层 — json_compat.py

### 解决的问题

`threads_meta.metadata_json` 等列是 JSON 类型，需要支持 `WHERE metadata_json->>'key' = 'value'` 语义的查询。但不同数据库的 JSON 查询语法完全不同：

| 操作 | SQLite | PostgreSQL |
|------|--------|------------|
| 获取 JSON 值类型 | `json_type(col, '$."key"')` | `json_typeof(col -> 'key')` |
| 提取 JSON 值 | `json_extract(col, '$."key"')` | `col ->> 'key'` |
| 类型名称 | `integer`, `real`, `text`, `null` | `number`, `string`, `boolean`, `null` |

`JsonMatch` 通过 SQLAlchemy 编译扩展机制，为每种方言生成对应的 SQL。

### 核心组件

#### `JsonMatch(ColumnElement)` — 自定义表达式

继承 `ColumnElement`（SQLAlchemy 自定义 SQL 表达式的标准方式），通过 `@compiles` 装饰器为不同方言注册编译函数。

构造参数：
- `column`: 要查询的 JSON 列
- `key`: JSON 对象中的键名
- `value`: 要匹配的值

构造时立即验证 `key` 和 `value` 的安全性。

#### 安全验证

**`validate_metadata_filter_key(key)`**：
- 键必须是 `[A-Za-z0-9_-]+` 模式的字符串
- 限制字符集因为键会被插入到 SQL 路径表达式中
- 防止 SQL/JSONPath 注入

**`validate_metadata_filter_value(value)`**：
- 允许类型：`None`, `bool`, `int`, `float`, `str`
- 整数限制在有符号 64 位范围 `[-2^63, 2^63-1]`
- 拒绝列表/字典/字节等无法安全编译为 SQL 谓词的类型

#### `_Dialect` — 方言配置

封装每种数据库在 JSON 类型/值比较时的配置差异：

| 配置项 | SQLite | PostgreSQL |
|--------|--------|------------|
| `null_type` | `"null"` | `"null"` |
| `num_types` | `("integer", "real")` | `("number",)` |
| `num_cast` | `"REAL"` | `"DOUBLE PRECISION"` |
| `int_types` | `("integer",)` | `("number",)` |
| `int_cast` | `"INTEGER"` | `"BIGINT"` |
| `int_guard` | `None` | `'^-?[0-9]+$'` |
| `string_type` | `"text"` | `"string"` |
| `bool_type` | `None` | `"boolean"` |

#### `_build_clause()` — 构建比较子句

根据 Python 值类型（None → bool → int → float → str）构建不同的 SQL 谓词：

| 值类型 | 逻辑 | 特殊处理 |
|--------|------|----------|
| `None` | `typeof = 'null'` | 直接比较 JSON 类型 |
| `bool` | 类型检查 + 值比较 | **必须在 int 前检查**（Python 中 bool 是 int 子类）；SQLite 直接比较字符串 |
| `int` | 类型检查 + 类型转换 + 值比较 | PostgreSQL 用正则防护区分浮点数 |
| `float` | 类型检查 + 类型转换 + 值比较 | — |
| `str` | 类型检查 + 直接比较 | — |

PostgreSQL 整数防护：`json_typeof = 'number'` 时值可能是 `1.5`（浮点数），直接 CAST 为 BIGINT 会报错。正则 `'^-?[0-9]+$'` 只匹配纯整数。

#### 编译函数

| 方言 | 函数 | 生成的 SQL 片段 |
|------|------|-----------------|
| SQLite | `_compile_sqlite` | `json_type(col, '$."key"')` + `json_extract(col, '$."key"')` |
| PostgreSQL | `_compile_pg` | `json_typeof(col -> 'key')` + `(col ->> 'key')` |
| 其他 | `_compile_default` | 抛出 `NotImplementedError` |

#### `json_match()` — 便捷工厂

```python
json_match(ThreadMetaRow.metadata_json, "status", "active")
# 等价于 SQL: metadata_json->>'status' = 'active'
```

### 使用位置

`JsonMatch` 目前只在 `ThreadMetaRepository.search()` 中使用，用于实现 `metadata` 参数的键值过滤。

## 4. 迁移环境 — migrations/env.py

### 职责

Alembic 迁移的环境配置。**只管理 DeerFlow 自己的表**（runs, threads_meta, run_events, feedback, users），不触碰 LangGraph 检查点的表。

### 两种迁移模式

| 模式 | 函数 | 用途 |
|------|------|------|
| Offline | `run_migrations_offline()` | 生成 SQL 脚本（不连接数据库），适合审查和手动执行 |
| Online | `run_migrations_online()` | 连接数据库执行迁移 |

### Batch 模式

`render_as_batch=True` 启用 batch 模式，原因是 SQLite 的 `ALTER TABLE` 支持非常有限。Batch 模式通过"创建新表 → 复制数据 → 删除旧表 → 重命名"的方式模拟完整的 `ALTER TABLE` 功能。

### 运行方式

Alembic 在模块级别根据 `context.is_offline_mode()` 自动选择模式：

```python
if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

在线模式使用 `run_sync` 将同步的 Alembic 迁移代码桥接到异步环境中。

## 5. 模型注册 — models/__init__.py

### 职责

集中导入所有 ORM 模型，确保 `Base.metadata` 包含完整的表定义。

**为什么需要**：SQLAlchemy 的 `Base.metadata` 只知道被**导入过**的模型。这个模块作为注册入口点，使 `init_engine` 的自动建表和 Alembic 的自动检测正常工作。

导入的模型：
- `FeedbackRow` — 反馈表
- `RunEventRow` — 运行事件表
- `RunRow` — 运行元数据表
- `ThreadMetaRow` — 线程元数据表
- `UserRow` — 用户表

## 6. 包入口 — __init__.py

### 公开 API

```python
from deerflow.persistence import init_engine, close_engine, get_session_factory, get_engine
```

只导出引擎生命周期管理函数。各 Repository 和 Store 需要通过各自的子包导入。
