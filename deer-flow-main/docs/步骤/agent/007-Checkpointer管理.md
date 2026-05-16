Checkpointer 管理——检查点注入时机与 LangGraph 自动管理机制

---

## 一、Checkpointer 不在 agent 构建中

Agent 的构建流程（`make_lead_agent()` → `create_agent()`）**不涉及 Checkpointer 的创建或配置**。Checkpointer 在更上层管理：

```
app 启动
  └─ langgraph_runtime(app)
      ├─ app.state.checkpointer = make_checkpointer()
      ├─ app.state.store = make_store()
      └─ app.state.stream_bridge = make_stream_bridge()
      └─ app.state.run_manager = RunManager()
```

---

## 二、注入时机

Checkpointer 通过 Runtime 注入机制传入 agent 执行：

```
每次请求
  └─ start_run() [services.py]
      └─ run_agent() [worker.py]
          ├─ Runtime 注入：将 thread context + store 塞入 config
          │   config["configurable"]["thread_id"] = thread_id
          │   config["store"] = store
          │
          └─ agent_factory(config) → make_lead_agent(config)
              └─ create_agent(..., checkpointer=None)  ← agent 构建不传 checkpointer
```

实际传入发生在 `agent.astream()` 调用时：

```python
# worker.py run_agent()
agent = agent_factory(config)
async for chunk in agent.astream(
    graph_input,
    config={
        "configurable": {"thread_id": thread_id, ...},
        ...,
    },
    stream_mode=stream_modes,
    # checkpointer 通过全局或 config 传入
):
    ...
```

LangGraph 的 `CompiledStateGraph.astream()` 在执行时自动通过 config 中的 `thread_id` 关联 checkpointer，读写检查点

---

## 三、检查点的读写

### 3.1 写入时机

LangGraph 在 `agent.astream()` 的每轮 ReAct 循环中自动写检查点：

```
LLM 调用 → 检查点写入（state 变更）
  → 检查是否需要工具 → 执行工具
  → 检查点写入（工具结果加入 state）
  → 再次 LLM 调用
  → 检查点写入
  → ...
```

每次 state 变更（新消息、工具结果、中间件修改的状态字段）都会触发检查点写入

### 3.2 读取时机

- `agent.astream()` 开始时：读取最新检查点恢复 state（支持续聊）
- `GET /api/threads/{id}/state`：通过 `checkpointer.aget_tuple()` 读取最新状态
- `POST /api/threads/{id}/history`：通过 `checkpointer.alist()` 读取检查点历史链

### 3.3 检查点内容

检查点存储的是完整的 `ThreadState`：

```python
class ThreadState(AgentState):
    sandbox: SandboxState | None           # 沙箱 ID
    thread_data: ThreadDataState | None    # 目录路径
    title: str | None                      # 自动标题
    artifacts: list[str]                   # 产物路径
    todos: list | None                     # 待办列表
    uploaded_files: list[dict] | None      # 上传文件
    viewed_images: dict                    # 图片缓存
    # + AgentState 的 messages（完整对话历史）
```

---

## 四、后端类型

**文件**：`agents/checkpointer/async_provider.py` → `make_checkpointer()`

与 Store 共享 `config.yaml` 中 `checkpointer` 配置段：

```python
@asynccontextmanager
async def make_checkpointer():
    config = get_app_config()

    if config.checkpointer is None:
        yield InMemorySaver()              # 未配置 → 内存
        return

    if config.checkpointer.type == "memory":
        yield InMemorySaver()
    elif config.checkpointer.type == "sqlite":
        async with AsyncSqliteSaver.from_conn_string(conn_str) as saver:
            await saver.setup()            # 自动建表
            yield saver
    elif config.checkpointer.type == "postgres":
        async with AsyncPostgresSaver.from_conn_string(conn_str) as saver:
            await saver.setup()            # 自动建表
            yield saver
```

所有后端的 `setup()` 方法自动创建所需的表结构，无需手动 migration

---

## 五、Checkpointer 与 Store 的关系

| | Checkpointer | Store |
|---|-------------|-------|
| 存什么 | 完整 state 快照（messages、artifacts 等） | 轻量元数据（thread_id、status、title） |
| 何时写 | LangGraph 每步自动写 | Gateway 手动写（创建 thread、同步 title） |
| 读场景 | 恢复状态、查看历史 | 列表查询、搜索 |
| 数据量 | 大（完整对话历史） | 小（几个字段） |
| 一致性 | 权威数据源 | 快速索引，最终与 checkpointer 一致 |

标题同步的闭环：

```
TitleMiddleware.after_model() → state.title = "..."
  → 写入 Checkpointer（自动）
  → 异步任务 _sync_thread_title_after_run()
    → 从 Checkpointer 读取 title
    → 更新 Store.values.title
    → /threads/search 返回正确标题
```

---

## 六、线程删除时的清理

```python
# threads.py:delete_thread_data()
# 1. 删除本地文件系统数据
_delete_thread_data(thread_id)

# 2. 删除 Store 记录（best-effort）
await store.adelete(THREADS_NS, thread_id)

# 3. 删除所有检查点（best-effort）
await checkpointer.adelete_thread(thread_id)
```

---

> 本文档：Checkpointer 在 app 启动时创建为全局单例，agent 构建时不管它。LangGraph 通过 config.thread_id 在 `astream()` 时自动读写检查点。每轮 ReAct 循环自动写入。后端支持 memory/sqlite/postgres，`setup()` 自动建表。Checkpointer 存完整状态，Store 存轻量索引，标题通过异步任务从前者同步到后者
