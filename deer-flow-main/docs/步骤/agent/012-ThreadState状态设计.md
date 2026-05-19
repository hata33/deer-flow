# ThreadState 状态设计

Agent 的运行时状态模式，定义了 ReAct 循环中哪些数据在节点间流转、如何归并。

---

## 一、状态在 Agent 中的位置

```python
# agent.py:374-380
return create_agent(
    model=create_chat_model(...),
    tools=get_available_tools(...),
    middleware=_build_middlewares(...),
    system_prompt=apply_prompt_template(...),
    state_schema=ThreadState,       # ← 这个
)
```

`ThreadState` 是 `create_agent` 的第五个参数。它定义了 LangGraph 图中所有节点（中间件、LLM、工具）之间共享的状态结构。每个节点读取和修改这个状态，LangGraph 的检查点机制也会持久化它。

---

## 二、继承关系

```
AgentState（LangChain 内置）
  └─ ThreadState（DeerFlow 扩展）
```

`AgentState` 只有一个字段：

```python
class AgentState(TypedDict):
    messages: list[BaseMessage]    # 对话历史，驱动 ReAct 循环
```

`ThreadState` 在此基础上扩展了 7 个字段。

---

## 三、字段详解

### 3.1 继承自 AgentState

| 字段 | 类型 | 说明 |
|------|------|------|
| `messages` | `list[BaseMessage]` | 对话历史，追加式增长，驱动 ReAct 循环 |

`messages` 是整个 Agent 的核心驱动力——LLM 输出追加到这里，工具结果追加到这里，循环直到 LLM 不再输出 `tool_calls`。

### 3.2 ThreadState 扩展字段

| 字段 | 类型 | 归并策略 | 写入者 | 说明 |
|------|------|---------|--------|------|
| `sandbox` | `SandboxState \| None` | 覆盖 | SandboxMiddleware | 沙箱 ID，工具执行时使用 |
| `thread_data` | `ThreadDataState \| None` | 覆盖 | ThreadDataMiddleware | 线程目录路径（workspace/uploads/outputs） |
| `title` | `str \| None` | 覆盖 | TitleMiddleware | 自动生成的线程标题 |
| `artifacts` | `list[str]` | **去重合并** | `present_files` 工具 | Agent 产出的文件路径列表 |
| `todos` | `list \| None` | 覆盖 | `write_todos` 工具 | 待办事项列表（plan mode） |
| `uploaded_files` | `list[dict] \| None` | 覆盖 | UploadsMiddleware | 本轮新上传的文件元数据 |
| `viewed_images` | `dict[str, ViewedImageData]` | **字典合并** | ViewImageMiddleware | 图片 base64 数据（image_path → {base64, mime_type}） |

---

## 四、两种归并策略

LangGraph 的状态更新不是简单的赋值，而是通过 **reducer** 函数归并。`ThreadState` 用了两种策略：

### 覆盖（默认）

```python
sandbox: NotRequired[SandboxState | None]
```

新值直接覆盖旧值。适用于状态是"当前值"而非"累积值"的字段：

- `sandbox`：只有一个当前沙箱
- `thread_data`：只有一个当前目录
- `title`：只有一个当前标题
- `todos`：整体替换待办列表
- `uploaded_files`：只关心本轮上传

### 自定义 reducer（Annotated）

```python
artifacts: Annotated[list[str], merge_artifacts]           # 去重合并
viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]  # 字典合并
```

**merge_artifacts** — 合并 + 去重，保持顺序：

```python
def merge_artifacts(existing, new):
    return list(dict.fromkeys(existing + new))  # 旧 + 新，去重保序
```

为什么用去重：`present_files` 可能多次被调用，重复展示同一文件没有意义。

**merge_viewed_images** — 字典合并，空字典清空：

```python
def merge_viewed_images(existing, new):
    if len(new) == 0:
        return {}           # 空字典 → 清空所有（处理完成后释放内存）
    return {**existing, **new}  # 合并，新值覆盖旧值
```

为什么可以清空：图片 base64 数据很大，用完后（LLM 已经看过）清空释放内存。

---

## 五、每个字段的读写关系

```
                    写入                          读取
                    ────                          ────
messages            LLM / 工具 / 中间件            LLM / 所有中间件
sandbox             SandboxMiddleware              工具（bash 等需要 sandbox_id）
thread_data         ThreadDataMiddleware            工具（路径翻译）、中间件
title               TitleMiddleware                 前端（via values 事件）、检查点
artifacts           present_files 工具              前端（via values 事件）
todos               write_todos 工具                前端（via values 事件）、LLM
uploaded_files      UploadsMiddleware               LLM（注入到消息中）
viewed_images       ViewImageMiddleware             LLM（注入 base64 图片）
```

---

## 六、状态在 ReAct 循环中的流转

```
before_agent:
  ThreadDataMiddleware → state.thread_data = {workspace_path, uploads_path, outputs_path}
  UploadsMiddleware → state.uploaded_files = [{filename, path, ...}]
  SandboxMiddleware → state.sandbox = {sandbox_id: "local"}

wrap_model_call:
  ViewImageMiddleware → state.viewed_images = {"/path/img.png": {base64, mime_type}}
  LLM 调用 → state.messages += [AIMessage(tool_calls=[...])]

after_model:
  SubagentLimitMiddleware → 截断 state.messages[-1].tool_calls

wrap_tool_call:
  present_files → state.artifacts += ["/mnt/user-data/outputs/result.md"]
  bash → 使用 state.sandbox.sandbox_id
  write_todos → state.todos = [{task, status, ...}]

after_agent:
  TitleMiddleware → state.title = "关于量子计算的讨论"
  MemoryMiddleware → 不改 state，排队异步记忆更新
```

---

## 七、状态与前端的关系

`agent.astream(stream_mode="values")` 每步产出完整的 `ThreadState` 快照。前端收到的每个 values 事件就是整个 state：

```json
{
  "messages": [...],
  "title": "关于量子计算的讨论",
  "artifacts": ["/mnt/user-data/outputs/report.md"],
  "todos": [{"task": "搜索资料", "status": "completed"}, {"task": "写报告", "status": "in_progress"}],
  "sandbox": {"sandbox_id": "local"},
  "thread_data": {"workspace_path": "...", "uploads_path": "...", "outputs_path": "..."},
  "uploaded_files": [],
  "viewed_images": {}
}
```

前端 `useStream` hook 通过 `thread.values` 访问这些字段：
- `messages` → 消息列表渲染
- `title` → 线程标题显示
- `artifacts` → 文件下载面板
- `todos` → 任务进度条

---

## 八、状态与检查点的关系

`ThreadState` 的完整内容（包括 messages 和所有扩展字段）会被 Checkpointer 持久化到磁盘/SQLite。下次恢复对话时，LangGraph 从检查点恢复整个 state，ReAct 循环从上次中断的地方继续。

```
Checkpointer.aget_tuple() → 完整 ThreadState（含 messages 历史 + sandbox + title + ...）
                           → 恢复后 agent 从这个状态继续执行
```

---

> 本文档：`ThreadState` 扩展 LangChain 的 `AgentState`，增加 sandbox、thread_data、title、artifacts、todos、uploaded_files、viewed_images 七个字段。`messages` 驱动 ReAct 循环，扩展字段通过中间件和工具写入。`artifacts` 用去重合并 reducer，`viewed_images` 用字典合并 reducer（支持清空释放内存）。完整状态通过 values 事件推给前端，通过 Checkpointer 持久化到磁盘。
