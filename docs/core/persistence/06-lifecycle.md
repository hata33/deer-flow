# Persistence 生命周期

本文描述持久化层从启动到关闭的完整生命周期，以及每个核心实体在其生命周期中的状态流转。

## 一、引擎生命周期

### 1.1 启动阶段（Gateway 启动时）

```
Gateway 启动
    │
    ▼
init_engine_from_config(config)
    │
    ▼
init_engine(backend, url, ...)
    │
    ├── backend == "memory"
    │   └── 跳过引擎创建，session_factory = None
    │
    ├── backend == "sqlite"
    │   ├── 创建 AsyncEngine (aiosqlite)
    │   ├── 注册 WAL 模式监听器（每个新连接执行 PRAGMA）
    │   └── 创建 session_factory
    │
    └── backend == "postgres"
        ├── 检查 asyncpg 驱动是否已安装
        ├── 创建 AsyncEngine (asyncpg + 连接池 + pool_pre_ping)
        └── 创建 session_factory
    │
    ▼
自动建表（Base.metadata.create_all）
    │
    ├── 成功 → 初始化完成
    │
    └── PostgreSQL "database does not exist"
        ├── _auto_create_postgres_db() — 自动创建数据库
        ├── 释放旧引擎
        ├── 重新创建引擎 + session_factory
        └── 再次尝试建表 → 初始化完成
```

**关键细节**：
- 引擎和会话工厂是 **全局单例**，整个进程共享
- SQLite WAL 模式在**每个新连接**上通过事件监听器启用（`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON`）
- PostgreSQL 的 `pool_pre_ping=True` 确保从连接池取出的连接都是活跃的
- 自动建表是**开发便利功能**，生产环境应使用 Alembic 迁移

### 1.2 运行阶段

```
各 Repository / Store 使用 get_session_factory() 获取会话工厂
    │
    ▼
每个方法通过 async with session_factory() as session 创建短生命周期会话
    │
    ├── 执行数据库操作（SELECT / INSERT / UPDATE / DELETE）
    ├── await session.commit()    — 提交事务
    └── 自动释放会话              — 退出 async with 块
```

**关键设计**：每个方法获取独立会话，操作完成即释放。这是因为后台工作线程可能运行数分钟，不能跨长时间持有连接。

### 1.3 关闭阶段（Gateway 关闭时）

```
Gateway 关闭
    │
    ▼
close_engine()
    │
    ├── await engine.dispose()  — 释放连接池中的所有连接
    ├── _engine = None
    └── _session_factory = None
```

## 二、Run 生命周期

```
用户发起对话请求
    │
    ▼
RunRepository.put()
    │ 创建 RunRow（status="pending"）
    │ 记录 thread_id, model_name, multitask_strategy 等
    │
    ▼
RunManager 开始执行
    │
    ▼
RunRepository.update_status(run_id, "running")
    │
    ▼
Agent 执行中...
    │
    ├── 可能: RunRepository.update_model_name() — 第一次 LLM 调用后确定实际模型
    │
    ├── RunJournal 在内存中持续累积:
    │   ├── Token 用量（按 caller 类型分类）
    │   ├── 消息计数
    │   └── 首尾消息摘要
    │
    ▼
执行完成（成功 / 失败 / 超时 / 中断）
    │
    ▼
RunRepository.update_run_completion()
    │ 一次性批量写入所有统计数据:
    │ ├── status → "success" | "error" | "timeout" | "interrupted"
    │ ├── Token 用量: total_input_tokens, total_output_tokens, total_tokens
    │ ├── 分类统计: lead_agent_tokens, subagent_tokens, middleware_tokens
    │ ├── LLM 调用次数: llm_call_count
    │ ├── 便利字段: message_count, first_human_message, last_ai_message
    │ └── error（如果失败）
    │
    ▼
后续查询:
    ├── RunRepository.get()          — 获取单个运行详情
    ├── RunRepository.list_by_thread() — 按线程列出运行
    ├── RunRepository.list_pending()   — 获取待处理运行队列
    └── RunRepository.aggregate_tokens_by_thread() — 线程级 Token 聚合
```

**状态流转**：`pending` → `running` → `success` | `error` | `timeout` | `interrupted`

## 三、RunEvent 生命周期

```
Run 开始执行
    │
    ▼
RunEventStore 开始记录事件
    │
    ├── seq=1:  lifecycle 事件 (event_type="run_started")
    ├── seq=2:  message 事件 (event_type="human", content="用户输入")
    ├── seq=3:  trace 事件 (event_type="tool_call", ...)
    ├── seq=4:  message 事件 (event_type="ai", content="AI 回复")
    ├── ...
    └── seq=N:  lifecycle 事件 (event_type="run_completed")
    │
    ▼
事件持久化到 run_events 表
    │ 唯一约束: (thread_id, seq) — 防止重复写入
    │ 复合索引: (thread_id, category, seq) — 按类型查询
    │           (thread_id, run_id, seq)   — 按运行查询
    │
    ▼
后续查询:
    ├── 按线程获取所有消息 (category="message")
    ├── 按运行获取所有事件
    └── 按序号范围分页查询
```

**事件分类**：
| category | 含义 | 示例 event_type |
|----------|------|-----------------|
| `message` | 对话消息 | `human`, `ai` |
| `trace` | 追踪信息 | `tool_call`, `tool_result`, `intermediate_step` |
| `lifecycle` | 生命周期 | `run_started`, `run_completed`, `run_cancelled` |

## 四、ThreadMeta 生命周期

```
Gateway 收到创建线程请求
    │
    ▼
ThreadMetaStore.create(thread_id, user_id=AUTO, ...)
    │
    ├── SQL 实现: ThreadMetaRepository.create()
    │   └── INSERT INTO threads_meta (thread_id, user_id, status="idle", ...)
    │
    └── Memory 实现: MemoryThreadMetaStore.create()
        └── store.aput(("threads",), thread_id, record)
    │
    ▼
线程活跃使用中
    │
    ├── TitleMiddleware 自动生成标题
    │   └── update_display_name(thread_id, "帮我写一篇关于AI的文章")
    │
    ├── 状态更新
    │   └── update_status(thread_id, "active")
    │
    ├── 自定义元数据更新（如 IM 频道信息）
    │   └── update_metadata(thread_id, {"channel": "feishu", "chat_id": "xxx"})
    │       └── read-modify-write: 读取 → 浅合并 → 写回
    │
    ├── 搜索线程
    │   └── search(metadata={"channel": "feishu"}, status="active")
    │       ├── SQL: json_match(metadata_json, "channel", "feishu")
    │       └── Memory: filter_dict={"channel": "feishu", "status": "active"}
    │
    └── 访问检查
        └── check_access(thread_id, user_id, require_existing=True)
            ├── 宽松模式 (require_existing=False): 记录不存在也返回 True
            └── 严格模式 (require_existing=True): 记录必须存在且所有者匹配
    │
    ▼
删除线程
    │
    ▼
ThreadMetaStore.delete(thread_id, user_id=AUTO)
    │ 先验证所有权，再删除
    │
    └── 同时清理本地文件系统中的线程目录
```

**策略选择**（`make_thread_store` 工厂函数）：
```
有 session_factory → ThreadMetaRepository (SQL)
只有 BaseStore    → MemoryThreadMetaStore (Memory)
都没有           → ValueError
```

## 五、Feedback 生命周期

```
运行完成，用户查看结果
    │
    ▼
用户点赞/点踩
    │
    ▼
FeedbackRepository.upsert(run_id, thread_id, rating=+1, user_id=AUTO)
    │
    ├── 查找 (thread_id, run_id, user_id) 组合的已有记录
    │
    ├── 已存在 → 更新 rating 和 comment，重置 created_at
    │
    └── 不存在 → 创建新记录 (feedback_id = uuid4())
    │
    ▼
用户修改反馈
    │
    └── 再次调用 upsert() — 覆盖之前的评分
    │
    ▼
用户添加评论
    │
    └── upsert(rating=+1, comment="回答很有帮助") 或 create(rating, comment)
    │
    ▼
用户删除反馈
    │
    └── FeedbackRepository.delete_by_run(thread_id, run_id, user_id=AUTO)
    │
    ▼
管理端查看统计
    │
    ├── aggregate_by_run() — 数据库端聚合：
    │   │ SELECT COUNT(*), SUM(CASE rating=1 ...), SUM(CASE rating=-1 ...)
    │   └── 返回 {total, positive, negative}
    │
    ├── list_by_thread_grouped() — 按运行分组返回所有反馈
    │
    └── list_by_run() — 列出某次运行的所有反馈
```

**唯一约束**：`(thread_id, run_id, user_id)` — 确保每个用户对同一运行只有一条反馈。

## 六、User 生命周期

```
用户注册
    │
    ├── 密码注册 → app 层的 SQLiteUserRepository 创建 UserRow
    └── OAuth 注册 → 查找/创建关联的 OAuth 账户
    │
    ▼
UserRow 写入 users 表
    │ id = UUID 字符串 (36字符)
    │ email (唯一)
    │ system_role = "user" | "admin"
    │ oauth_provider + oauth_id (部分唯一索引，仅约束非 NULL 行)
    │
    ▼
用户登录 → JWT Token 签发
    │ token_version 字段用于强制登出（递增使旧 Token 失效）
    │
    ▼
认证中间件 → 所有 Repository 方法的 user_id 参数
    │ 通过 contextvar 传递，resolve_user_id() 解析
    │
    ▼
运行创建 → RunRow.user_id = resolved_user_id
线程创建 → ThreadMetaRow.user_id = resolved_user_id
反馈创建 → FeedbackRow.user_id = resolved_user_id
```

> User 的 Repository 实现在 app 层（`app.gateway.auth.repositories.sqlite`），因为需要在 ORM 行和 auth 模块的 Pydantic User 类之间转换。ORM 模型定义在 harness 层是为了确保 `Base.metadata.create_all()` 能发现并创建 users 表。

## 七、迁移生命周期

```
开发者修改 ORM 模型（新增字段、新表等）
    │
    ▼
alembic revision --autogenerate -m "描述"
    │
    │ migrations/env.py 执行流程:
    │ 1. 导入所有模型 → Base.metadata 包含完整表定义
    │ 2. 比较模型定义与数据库实际 schema
    │ 3. 生成迁移脚本
    │
    ▼
alembic upgrade head
    │
    ├── offline 模式: 只生成 SQL 脚本（不连接数据库）
    └── online 模式:  连接数据库并执行迁移
    │   └── render_as_batch=True — SQLite 兼容（新表→复制→删旧→重命名）
    │
    ▼
数据库 schema 更新完成
```

## 八、JSON 兼容层的编译生命周期

```
用户请求: 搜索 metadata 中 channel="feishu" 的线程
    │
    ▼
ThreadMetaRepository.search(metadata={"channel": "feishu"})
    │
    ▼
json_match(ThreadMetaRow.metadata_json, "channel", "feishu")
    │
    ├── validate_metadata_filter_key("channel") → True
    └── validate_metadata_filter_value("feishu") → True
    │
    ▼
JsonMatch(column, "channel", "feishu") 加入 WHERE 子句
    │
    ▼
SQLAlchemy 编译时根据方言分派:
    │
    ├── SQLite:
    │   WHERE (json_type(metadata_json, '$."channel"') = 'text'
    │          AND json_extract(metadata_json, '$."channel"') = :param)
    │
    └── PostgreSQL:
        WHERE (json_typeof(metadata_json -> 'channel') = 'string'
               AND (metadata_json ->> 'channel') = :param)
    │
    ▼
数据库执行查询，返回匹配的线程
```

**类型安全**：`_build_clause` 根据 Python 值类型（None / bool / int / float / str）构建不同的 SQL 谓词，确保 JSON 列中的类型精确匹配。例如 `bool` 在 `int` 之前检查（因为 Python 中 `bool` 是 `int` 的子类），`int` 值在 PostgreSQL 上用正则防护区分浮点数。
