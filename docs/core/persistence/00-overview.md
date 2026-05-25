# Persistence 持久化层 — 全局概览

## 定位

DeerFlow 持久化层（`deerflow.persistence`）是 DeerFlow 应用数据的统一存储抽象。它负责管理 **Run 运行元数据**、**Thread 线程元数据**、**Feedback 用户反馈**、**RunEvent 运行事件** 和 **User 用户信息** 五大核心实体的完整生命周期。

> **关键边界**：持久化层与 LangGraph 检查点（checkpointer）完全独立。LangGraph 检查点负责管理图执行状态的快照与恢复，有独立的 schema 和迁移生命周期，不由本层管理。

## 解决的核心问题

| 问题 | 持久化层的解决方案 |
|------|---------------------|
| **应用数据无处安放** | LangGraph 只管图执行状态，不关心"谁的运行"、"跑了多少 token"、"用户如何评价"等应用层信息。持久化层填补了这个空白 |
| **多后端兼容** | 支持 memory（开发调试）、SQLite（单节点部署）、PostgreSQL（生产集群）三种后端，上层代码无需关心差异 |
| **JSON 字段跨库查询** | `metadata_json` 等字段需要在 SQLite 和 PostgreSQL 上执行相同的键值匹配查询，但二者 JSON 语法完全不同。`json_compat` 模块通过 SQLAlchemy 编译扩展抹平差异 |
| **用户所有权隔离** | 所有实体都带有 `user_id` 字段，所有读写操作均包含三态所有权过滤（AUTO / 显式值 / None 绕过），实现多租户数据隔离 |
| **会话生命周期管理** | 后台工作线程可能运行数分钟，不能跨长时间持有连接。持久化层采用短生命周期会话模式，每个方法获取独立会话，操作完成即释放 |

## 能力来源

持久化层的能力建立在以下技术栈之上：

```
SQLAlchemy 2.0 异步 ORM
  ├── AsyncEngine           — 异步数据库引擎（asyncpg / aiosqlite）
  ├── async_sessionmaker    — 异步会话工厂（短生命周期会话）
  ├── DeclarativeBase       — 声明式基类（自动 to_dict 序列化）
  └── 自定义编译扩展          — JsonMatch（方言可移植的 JSON 查询）

Alembic                    — 数据库迁移（batch 模式兼容 SQLite ALTER TABLE 限制）

LangGraph BaseStore        — 内存模式的底层键值存储（仅 ThreadMeta 使用）
```

## 架构总览

```
persistence/
├── __init__.py              # 公开 API 入口（init_engine, close_engine, get_session_factory）
├── engine.py                # 引擎生命周期管理（创建/关闭/自动建表）
├── base.py                  # ORM 声明基类（Base，自动 to_dict）
├── json_compat.py           # 跨方言 JSON 查询引擎（JsonMatch）
├── models/
│   ├── __init__.py          # 模型注册入口点（确保 Base.metadata 包含所有表）
│   └── run_event.py         # RunEventRow — 运行事件表
├── run/
│   ├── model.py             # RunRow — 运行元数据表
│   └── sql.py               # RunRepository — RunStore 的 SQL 实现
├── feedback/
│   ├── model.py             # FeedbackRow — 反馈表
│   └── sql.py               # FeedbackRepository — 反馈数据仓库
├── thread_meta/
│   ├── base.py              # ThreadMetaStore 抽象接口 + InvalidMetadataFilterError
│   ├── model.py             # ThreadMetaRow — 线程元数据表
│   ├── sql.py               # ThreadMetaRepository — SQL 实现
│   ├── memory.py            # MemoryThreadMetaStore — 内存实现（LangGraph BaseStore）
│   └── __init__.py          # make_thread_store 工厂函数
├── user/
│   └── model.py             # UserRow — 用户表（无 Repository，由 app 层实现）
└── migrations/
    └── env.py               # Alembic 迁移环境配置
```

## 五大核心实体

### 1. Run（运行元数据）
- **表**: `runs`
- **职责**: 记录每次 Agent 运行的汇总信息——状态、模型、Token 用量统计、首尾消息摘要
- **特点**: 包含按调用方类型分类的 Token 统计（lead_agent / subagent / middleware），支持成本分析
- **详见**: [01-run.md](01-run.md)

### 2. RunEvent（运行事件）
- **表**: `run_events`
- **职责**: 存储运行过程中的所有事件流——对话消息、追踪信息、生命周期事件
- **特点**: 事件按 `(thread_id, seq)` 唯一排序，支持按线程+分类、按线程+运行两种查询模式
- **详见**: [02-run-event.md](02-run-event.md)

### 3. ThreadMeta（线程元数据）
- **表**: `threads_meta`
- **职责**: 管理线程的标题、状态、所有者和自定义元数据
- **特点**: 唯一采用策略模式的子模块——提供 SQL 和 Memory 两种实现；`metadata_json` 列支持跨方言的 JSON 键值过滤搜索
- **详见**: [03-thread-meta.md](03-thread-meta.md)

### 4. Feedback（用户反馈）
- **表**: `feedback`
- **职责**: 存储用户对运行结果的评价——点赞/点踩、评论、针对特定消息的反馈
- **特点**: 唯一约束 `(thread_id, run_id, user_id)` 支持 upsert 语义；提供数据库端聚合统计
- **详见**: [04-feedback.md](04-feedback.md)

### 5. User（用户信息）
- **表**: `users`
- **职责**: 存储用户账户信息——密码认证、OAuth 认证、角色管理
- **特点**: ORM 模型在 harness 层定义但 Repository 在 app 层实现，保持 harness 对 app 的独立性
- **详见**: [05-user.md](05-user.md)

## 三后端支持

| 后端 | 引擎 | 连接池 | 适用场景 |
|------|------|--------|----------|
| `memory` | 不创建引擎 | 无 | 开发调试、无数据库环境 |
| `sqlite` | aiosqlite + WAL 模式 | 单连接 | 单节点部署、本地开发 |
| `postgres` | asyncpg + 连接池 | pool_size (默认 5) | 生产集群、多实例部署 |

后端选择通过 `config.yaml` 的 `database.backend` 字段配置，`init_engine` 根据配置创建对应的引擎。

## 所有权三态语义

所有持久化操作中的 `user_id` 参数遵循统一的三态语义：

| 值 | 行为 | 适用场景 |
|----|------|----------|
| `AUTO`（默认） | 从请求作用域的 contextvar 自动解析当前用户 ID | 正常请求处理 |
| 显式 `str` | 使用提供的用户 ID | 测试、跨线程操作 |
| 显式 `None` | 绕过所有权过滤 | 迁移脚本、CLI 工具、管理操作 |

这一设计由 `deerflow.runtime.user_context.resolve_user_id()` 统一实现，所有 Repository 方法在执行前都会调用它解析用户身份。

## 与其他系统的关系

```
                    ┌──────────────────────────┐
                    │      Gateway API         │
                    │  (FastAPI Routers)       │
                    └────────┬─────────────────┘
                             │ 调用
                    ┌────────▼─────────────────┐
                    │   Persistence Layer       │ ◄── 本文档范围
                    │  (Repository / Store)     │
                    └────────┬─────────────────┘
                             │ 依赖
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌──────────┐  ┌──────────┐  ┌──────────────┐
        │ SQLAlchemy│  │ LangGraph │  │ Alembic      │
        │ AsyncEngine│ │ BaseStore │  │ Migrations   │
        └──────────┘  └──────────┘  └──────────────┘

        ┌──────────────────────────────────────────────┐
        │        LangGraph Checkpointer                │
        │  （独立于持久化层，管理图执行状态）              │
        └──────────────────────────────────────────────┘
```

## 相关文档

- [06-lifecycle.md](06-lifecycle.md) — 完整的持久化层生命周期
- [07-infrastructure.md](07-infrastructure.md) — 引擎管理、基类、JSON 兼容层等基础设施
