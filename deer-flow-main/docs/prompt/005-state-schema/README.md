# 005-状态模式模块

> 已验证来源：deer-flow 项目 `agents/thread_state.py` + `agents/middlewares/thread_data_middleware.py` + `agents/middlewares/uploads_middleware.py` + `agents/middlewares/view_image_middleware.py` + `agents/middlewares/title_middleware.py` + `agents/middlewares/clarification_middleware.py`
> 本提示词可在新项目中直接使用，通过适配层注入新项目的状态字段需求，不需要修改本提示词本体。

---

## 一、设计意图

**为什么需要这个模块？**

Agent 运行时需要一个共享状态字典，在 LLM 调用、工具执行、中间件处理之间传递数据。裸 dict 无类型约束、无冲突处理——两个工具同时写 `artifacts` 列表会互相覆盖。TypedDict + Annotated reducer 让每个字段有明确类型，多写操作自动合并而非覆盖。

**解决的核心痛点：**
- 并发状态写冲突 → Annotated reducer 自动合并
- 无类型约束拼错字段名 → TypedDict 编译时检查
- 可选字段必须初始化 → NotRequired 标记延迟初始化
- 子字段命名冲突 → 独立 TypedDict 子结构
- 图片重复注入 LLM → 空字典清空语义

---

## 二、输入契约

状态由框架、中间件、工具三方写入：

| 写入方 | 字段 | 时机 |
|--------|------|------|
| 框架 | `messages` | 每轮 LLM 调用 / 工具执行 |
| ThreadDataMiddleware | `sandbox`, `thread_data` | `before_agent` |
| present_file_tool | `artifacts` | 工具执行时 |
| view_image_tool | `viewed_images` | 工具执行时 |
| UploadsMiddleware | `uploaded_files`, `messages` | `before_agent` |
| TitleMiddleware | `title` | `after_model`（首次） |

---

## 三、输出契约

### 状态结构

```python
class ThreadState(AgentState):
    sandbox: NotRequired[SandboxState | None]           # 沙箱标识
    thread_data: NotRequired[ThreadDataState | None]     # 目录路径
    title: NotRequired[str | None]                       # 自动标题
    artifacts: Annotated[list[str], merge_artifacts]     # 产物（去重保序）
    todos: NotRequired[list | None]                       # 待办
    uploaded_files: NotRequired[list[dict] | None]       # 上传文件
    viewed_images: Annotated[dict, merge_viewed_images]  # 图片（合并/清空）
```

### Reducer 保证

| Reducer | 行为 |
|---------|------|
| `merge_artifacts` | 合并两个列表，`dict.fromkeys` 去重保序。`None` 输入返回另一个 |
| `merge_viewed_images` | 字典合并，新覆盖旧。`new == {}` 时清空全部 |

### 字段生命周期

```
before_agent: ThreadDataMiddleware → sandbox, thread_data
              UploadsMiddleware → uploaded_files, messages
    ↓
LLM 调用 / 工具执行循环:
    工具 → artifacts, viewed_images, messages
    ViewImageMiddleware → messages（图片注入）, viewed_images（清空）
    ↓
after_model: TitleMiddleware → title（首次）
```

---

## 四、行为约束

### 约束 1：必须继承框架的 AgentState

```python
# 正确
class ThreadState(AgentState):
    ...

# 错误：自己定义 messages
class ThreadState(TypedDict):
    messages: list[BaseMessage]  # 与框架 create_agent 不兼容
```

### 约束 2：并发写字段必须用 reducer

```python
# 正确：Annotated + reducer
artifacts: Annotated[list[str], merge_artifacts]

# 错误：无 reducer，工具返回的 artifacts 直接覆盖前值
artifacts: list[str]
```

### 约束 3：可选字段用 NotRequired

不标记则每次构造 state 都必须传全部字段，即使大部分字段在首次请求时不存在。

### 约束 4：中间件声明最小状态子集

每个中间件只声明它读写的字段，不直接用 ThreadState。TypedDict 是结构化类型，字段兼容即可。

### 约束 5：viewed_images 必须清空

ViewImageMiddleware 注入图片到 LLM 消息后，必须清空 `viewed_images`。不清空则每轮 LLM 调用重复注入历史图片。

### 约束 6：title 只写一次

`state.get("title")` 已存在时跳过。多轮对话不应覆盖首次生成的标题。

---

## 五、验证场景

| # | Given | When | Then |
|---|-------|------|------|
| 1 | 两个工具同时返回 artifacts | reducer 执行 | 合并去重保序 |
| 2 | artifacts existing=None | reducer 执行 | 返回 new |
| 3 | artifacts new=None | reducer 执行 | 返回 existing |
| 4 | viewed_images new={} | reducer 执行 | 返回 {}（清空） |
| 5 | viewed_images 新旧同 key | reducer 执行 | 新值覆盖 |
| 6 | state 不传 sandbox | 构造 | 不报错 |
| 7 | title 已存在 | TitleMiddleware | 跳过 |
| 8 | 上一轮无 view_image 调用 | ViewImageMiddleware | 跳过 |
| 9 | ViewImageMiddleware 已注入 | 第二次 before_model | 跳过（检查标记） |

---

## 六、自由度与禁区

### 可以改的

- 状态字段（按业务需求增减）
- 子结构内容（SandboxState 加新字段）
- 中间件实现（不同的标题生成策略）
- reducer 逻辑（不同的合并策略）
- 目录初始化时机（lazy vs eager）

### 不能改的

- **必须继承 AgentState**：自己定义 messages 不兼容
- **并发写字段必须用 reducer**：否则覆盖丢失
- **可选字段用 NotRequired**：否则构造时必须传全部
- **中间件声明最小子集**：不应感知不相关字段
- **viewed_images 清空语义**：不清空则图片重复注入
- **merge_artifacts 去重保序**：用 set 会丢顺序

---

## 七、依赖的上下游模块

```
[无上游] 状态模式是底层定义
    ↓
[下游] Agent 工厂 → 传 state_schema=create_agent
[下游] 工具系统 → Command(update={"artifacts": ..., "viewed_images": ...})
[下游] 中间件系统 → 读写各状态字段
[下游] Agent Loop → state["messages"] 驱动循环
[下游] Checkpointer → 持久化整个 state
```
