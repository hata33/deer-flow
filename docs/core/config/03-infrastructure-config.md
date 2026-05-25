# 基础设施配置

本篇覆盖配置系统中与基础设施相关的配置：数据库、沙箱、Checkpointer、Stream Bridge、运行事件、路径管理和追踪。

## 一、数据库配置（database_config.py）

### 三种后端

| 后端 | Checkpointer | 应用 ORM | 适用场景 |
|------|-------------|----------|----------|
| `memory` | MemorySaver | 内存存储 | 开发/测试 |
| `sqlite` | 共享 .db 文件（WAL） | 同一 .db 文件 | 单节点部署 |
| `postgres` | 独立连接池 | 独立连接池 | 生产多节点 |

### SQLite 模式详解

```
{sqlite_dir}/deerflow.db
    ├── 启用 WAL 日志模式 → 并发读 + 单写
    ├── busy timeout 5 秒 → 写冲突等待而非失败
    └── checkpointer_sqlite_path = app_sqlite_path = sqlite_path（共享）
```

### PostgreSQL 模式

```
postgres_url: $DATABASE_URL
    ├── 自动添加 +asyncpg 驱动后缀
    ├── app_sqlalchemy_url → SQLAlchemy 异步引擎
    └── checkpointer 创建独立连接池
```

### 派生属性

```python
database.sqlite_path          → "{sqlite_dir}/deerflow.db"
database.app_sqlalchemy_url   → "sqlite+aiosqlite:///{path}" 或 "postgresql+asyncpg://..."
```

## 二、沙箱配置（sandbox_config.py）

### 通用字段

| 字段 | 说明 |
|------|------|
| `use` | Provider 类路径（必需） |
| `allow_host_bash` | 允许本地沙箱在宿主机执行 bash（危险！） |

### AioSandboxProvider 专用

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `image` | — | Docker 镜像 |
| `replicas` | 3 | 最大并发容器数（LRU 淘汰） |
| `idle_timeout` | 600 | 空闲超时秒数（0=不超时） |
| `mounts` | [] | 额外卷挂载 |
| `environment` | {} | 注入到容器的环境变量 |

### 输出截断

| 字段 | 默认值 | 截断策略 |
|------|--------|----------|
| `bash_output_max_chars` | 20000 | 中间截断（保留头尾） |
| `read_file_output_max_chars` | 50000 | 头部截断 |
| `ls_output_max_chars` | 20000 | 头部截断 |

## 三、Checkpointer 配置（checkpointer_config.py）

独立于 DatabaseConfig。当用户未显式配置时，运行时根据 DatabaseConfig.backend 自动推断。

| 后端 | connection_string | 说明 |
|------|-------------------|------|
| `memory` | — | 进程内，重启丢失 |
| `sqlite` | 可选，默认 store.db | 文件路径或 :memory: |
| `postgres` | 必需 | DSN 字符串 |

### 与 DatabaseConfig 的关系

```
用户配置了 checkpointer 字段？
    ├── 是 → 使用显式配置
    └── 否 → 根据 database.backend 推断
        ├── memory → MemorySaver
        ├── sqlite → SqliteSaver(database.sqlite_path)
        └── postgres → PostgresSaver(database.postgres_url)
```

## 四、Stream Bridge 配置（stream_bridge_config.py）

| 字段 | 说明 |
|------|------|
| `type` | memory（进程内 Queue）或 redis（Redis Streams，未实现） |
| `redis_url` | Redis URL（仅 redis 类型） |
| `queue_maxsize` | 每个运行的最大缓冲事件数 |

## 五、运行事件配置（run_events_config.py）

| 字段 | 说明 |
|------|------|
| `backend` | memory / db / jsonl |
| `max_trace_content` | 追踪内容最大字节数（db 后端） |
| `track_token_usage` | 是否累积 token 到 RunRow |

## 六、路径管理（paths.py + runtime_paths.py）

### 路径解析层次

```
runtime_paths.py
    ├── project_root()    → CWD 或 DEER_FLOW_PROJECT_ROOT
    └── runtime_home()    → DEER_FLOW_HOME 或 {project_root}/.deer-flow

paths.py (Paths 类)
    ├── base_dir          → 构造参数 或 DEER_FLOW_HOME 或 runtime_home()
    └── host_base_dir     → DEER_FLOW_HOST_BASE_DIR 或 base_dir
```

### 目录布局

```
{base_dir}/
├── memory.json, USER.md
├── agents/                      ← 旧布局（只读回退）
├── users/{user_id}/
│   ├── memory.json
│   ├── agents/{name}/
│   └── threads/{thread_id}/
│       ├── user-data/
│       │   ├── workspace/       ← /mnt/user-data/workspace/
│       │   ├── uploads/         ← /mnt/user-data/uploads/
│       │   └── outputs/         ← /mnt/user-data/outputs/
│       └── acp-workspace/       ← /mnt/acp-workspace/
└── threads/{thread_id}/         ← 旧布局（无用户隔离）
```

### Docker/DooD 路径处理

```
本地执行:
    base_dir = /home/user/project/.deer-flow
    host_base_dir = /home/user/project/.deer-flow（相同）

DooD 模式:
    容器内 base_dir = /app/.deer-flow
    宿主机 host_base_dir = /home/user/project/.deer-flow（通过 DEER_FLOW_HOST_BASE_DIR）
    Docker 守护进程用 host_base_dir 创建卷挂载
```

### 虚拟路径解析

```
resolve_virtual_path(thread_id, "/mnt/user-data/outputs/report.pdf")
    │
    ├── 检查前缀匹配（精确段边界）
    ├── 提取相对路径: outputs/report.pdf
    ├── 拼接: {thread_dir}/user-data/outputs/report.pdf
    ├── resolve() → 绝对路径
    └── 路径遍历检测: actual relative_to base
```

### 安全验证

- thread_id 和 user_id 通过正则验证：`^[A-Za-z0-9_\-]+$`
- 目录创建权限 0o777（确保沙箱容器能写入）
- 虚拟路径解析检测 `..` 遍历攻击

## 七、追踪配置（tracing_config.py）

### 配置来源

所有配置从环境变量读取（不从 config.yaml）：

| Provider | 启用 | 凭证 |
|----------|------|------|
| LangSmith | LANGSMITH_TRACING, LANGCHAIN_TRACING_V2 | LANGSMITH_API_KEY, LANGCHAIN_API_KEY |
| Langfuse | LANGFUSE_TRACING | LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY |

### 线程安全

使用 double-check locking 懒加载全局单例：
```python
if _tracing_config is not None:     # 快速路径（无锁）
    return _tracing_config
with _config_lock:                   # 慢速路径（加锁）
    if _tracing_config is not None:  # 再次检查
        return _tracing_config
    _tracing_config = TracingConfig(...)
```

### 双重检查

```python
enabled: bool           # 用户显式启用（即使配置不完整）
is_configured: bool     # enabled 且凭证完整（可以实际工作）
validate_enabled()      # 启用但缺凭证 → ValueError
```
