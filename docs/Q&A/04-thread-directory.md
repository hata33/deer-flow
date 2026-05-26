# Q&A 04: 线程目录

> "创建线程目录"是什么意思？是指线程的命名和组织结构，还是某种线程注册管理机制？

---

## 答案：两者兼有

DeerFlow 的"线程目录"是一个**双轨机制**：

1. **文件系统目录** — 每个 thread 在磁盘上有独立的工作空间
2. **元数据注册表** — SQL/内存中的线程索引

---

## 一、文件系统目录

### 目录结构

```
{workspace}/
└── users/
    └── {user_id}/
        └── threads/
            └── {thread_id}/
                ├── user-data/       ← 沙箱内挂载为 /mnt/user-data/
                │   ├── workspace/   ← 工作文件
                │   ├── uploads/     ← 用户上传的文件
                │   └── outputs/     ← Agent 生成的输出
                └── acp-workspace/   ← ACP 协议工作空间
```

### 创建时机

`ThreadDataMiddleware` 在中间件链的**第一位**执行（`before_agent` 钩子），确保每次 Agent 运行前线程目录已就绪：

```python
# middlewares/thread_data_middleware.py
def before_agent(self, state, runtime):
    # 确保线程目录存在
    ensure_thread_dirs(thread_id)
    # → 创建 user-data/, uploads/, outputs/ 等子目录
```

### 安全措施

```python
# thread_id 格式校验 — 防止路径穿越
# 只允许字母、数字、下划线、连字符
pattern = re.compile(r"^[A-Za-z0-9_\-]+$")
```

---

## 二、元数据注册表

### 接口定义

```python
class ThreadMetaStore(ABC):
    async def create(self, thread_id, user_id, metadata): ...
    async def get(self, thread_id) -> ThreadMeta: ...
    async def search(self, query) -> list[ThreadMeta]: ...
    async def update_metadata(self, thread_id, metadata): ...
    async def delete(self, thread_id): ...
```

### 两种实现

| 实现 | 存储 | 适用场景 |
|------|------|---------|
| `ThreadMetaRepository` | SQLite/PostgreSQL | 生产环境 |
| `MemoryThreadMetaStore` | LangGraph BaseStore（内存） | 开发/测试 |

### 注册流程

```
Frontend 创建线程 → POST /api/langgraph/threads
                         ↓
                  LangGraph Server 创建线程状态
                         ↓
                  Gateway ThreadMetaStore.create()
                         ↓
                  注册元数据（owner_id, user_id, created_at）
```

---

## 三、用户隔离

线程按 `user_id` 隔离：

```
文件系统: {workspace}/users/{user_id}/threads/{thread_id}/
元数据:   owner_id = user_id（服务端保留字段）
```

**安全控制**:
- `owner_id` 和 `user_id` 是服务端保留的 metadata key
- 客户端无法伪造（`_strip_reserved_metadata()` 过滤）
- 每次访问线程都检查 `check_access(user_id, thread_id)`

---

## 四、线程与沙箱的关系

每个线程有独立的沙箱环境：

```
线程目录（宿主机）                    沙箱内虚拟路径
{workspace}/users/{uid}/threads/{tid}/user-data/  →  /mnt/user-data/
{workspace}/users/{uid}/threads/{tid}/user-data/uploads/  →  /mnt/user-data/uploads/
```

`PathMapping` 负责双向翻译。Agent 在沙箱内通过 `/mnt/user-data/...` 访问文件，系统自动映射到宿主机的线程目录。

---

## 相关源码

| 组件 | 文件 |
|------|------|
| 线程目录创建 | `backend/packages/harness/deerflow/agents/middlewares/thread_data_middleware.py` |
| 路径配置 | `backend/packages/harness/deerflow/config/paths.py` |
| 元数据存储 | `backend/packages/harness/deerflow/persistence/thread_meta/` |
| 线程路由 | `backend/app/gateway/routers/threads.py` |
| 虚拟路径映射 | `backend/packages/harness/deerflow/sandbox/` |

## 深入阅读

- [Sandbox 设计决策](../docs/core/sandbox/05-design-decisions.md)
- [持久化设计决策](../docs/core/persistence/08-design-decisions.md)
- [文件上传全流程](../docs/lifecycle/06-file-upload.md)
