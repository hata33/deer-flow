# 005-状态模式

## 解决什么问题

Agent 运行时需要一个共享状态字典，在 LLM 调用、工具执行、中间件处理之间传递数据。
裸 dict 无类型约束、无冲突处理——两个工具同时写 `artifacts` 列表会互相覆盖。
TypedDict + Annotated reducer 让每个字段有明确类型，多写操作自动合并而非覆盖。

## 本模块的职责边界

**只定义状态结构和合并策略**：ThreadState 的字段类型、每个可写字段的 reducer 函数。
不负责：状态的初始化（ThreadDataMiddleware）、状态的消费（各中间件和工具）、状态的持久化（Checkpointer）。

## 不可变的设计决策

**TypedDict 继承 AgentState，不自己定义 messages**：`ThreadState(AgentState)` 继承框架的 `messages: list[BaseMessage]`。
自己定义则与框架的 `create_agent` 不兼容——它硬编码读 `state["messages"]`。

**Annotated + reducer 函数处理并发写**：
- `artifacts: Annotated[list[str], merge_artifacts]` — 多个工具返回的产物路径合并并去重（`dict.fromkeys` 保序）。
- `viewed_images: Annotated[dict, merge_viewed_images]` — 图片字典合并，新值覆盖同名旧值。空字典 `{}` 是特殊信号，表示清空全部。
不用 reducer 的话，工具通过 `Command(update={"artifacts": ...})` 返回的状态会直接覆盖前值，丢失其他工具的产物。

**NotRequired 标记可选字段**：`sandbox`、`thread_data`、`title`、`todos`、`uploaded_files` 都是 `NotRequired`。
第一个请求到达时这些字段不存在，中间件在 `before_agent` 中初始化它们。不用 `NotRequired` 则每次构造 state 都必须传全部字段。

**子结构用独立 TypedDict**：`SandboxState`、`ThreadDataState`、`ViewedImageData` 各自是一个 TypedDict。
不用扁平字符串——`sandbox_id` 放在 `sandbox` 子结构中，与 `thread_data` 的路径字段隔离，避免命名冲突。

**中间件各自声明最小状态子集**：`ViewImageMiddlewareState` 只声明 `viewed_images`，`ThreadDataMiddlewareState` 只声明 `thread_data`。
不直接用 `ThreadState`——中间件不应感知不相关的字段。由于 TypedDict 是结构化类型（structural typing），只要字段兼容就能互换。

**merge_artifacts 用 dict.fromkeys 去重保序**：`list(dict.fromkeys(existing + new))`。不用 `set` 因为会丢失插入顺序——前端按顺序展示产物列表。

**merge_viewed_images 空字典清空语义**：`new == {}` 时返回 `{}`，不是 `{**existing, **new}`。
中间件在图片注入到 LLM 消息后清空 `viewed_images`，防止下一轮重复注入。不清空的话每轮 LLM 调用都会看到所有历史图片，浪费 token。

**title 字段只写一次**：TitleMiddleware 检查 `state.get("title")` 是否已存在，存在则跳过。
标题在首次对话后生成，后续轮次不再覆盖。

**thread_data 的 lazy_init 模式**：`lazy_init=True` 时只计算路径字符串，不创建目录。
目录由沙箱工具在首次使用时通过 `ensure_thread_directories_exist` 创建。好处：无文件操作的对话不会触发磁盘 I/O。

## 适配层

```yaml
<ADAPT>
# === 框架 ===
agent_state_type: "AgentState"              # LangGraph 的基础状态类型
state_schema: "TypedDict"                   # 状态定义方式
reducer_annotation: "Annotated[field, fn]"  # reducer 标注方式

# === 状态字段（按需增减）===
state_fields:
  # 框架基础字段
  messages:
    type: "list[BaseMessage]"
    reducer: "框架内置（追加）"

  # 沙箱状态
  sandbox:
    type: "SandboxState | None"
    init: "ThreadDataMiddleware.before_agent"
    consumers: ["sandbox tools", "SandboxAuditMiddleware"]

  # 线程数据目录
  thread_data:
    type: "ThreadDataState | None"
    init: "ThreadDataMiddleware.before_agent"
    consumers: ["sandbox tools", "path resolution"]

  # 自动标题
  title:
    type: "str | None"
    init: "TitleMiddleware.after_model（首次）"
    consumers: ["前端显示"]

  # 产物路径
  artifacts:
    type: "list[str]"
    reducer: "merge_artifacts（去重保序）"
    writers: ["present_file_tool"]
    consumers: ["前端展示"]

  # 待办列表
  todos:
    type: "list | None"
    consumers: ["前端展示"]

  # 上传文件
  uploaded_files:
    type: "list[dict] | None"
    init: "UploadsMiddleware.before_agent"
    consumers: ["UploadsMiddleware（注入提示词）"]

  # 已查看图片
  viewed_images:
    type: "dict[str, ViewedImageData]"
    reducer: "merge_viewed_images（合并/清空）"
    writers: ["view_image_tool"]
    consumers: ["ViewImageMiddleware（注入 LLM 消息）"]
</ADAPT>
```

## 自检清单

| # | 验证 | 期望 |
|---|------|------|
| 1 | 两个工具同时返回 artifacts | 合并去重，顺序保留 |
| 2 | artifacts 无 existing | 返回 new |
| 3 | artifacts 无 new | 返回 existing |
| 4 | viewed_images new={} | 返回 {}（清空） |
| 5 | viewed_images 新旧有同 key | 新值覆盖 |
| 6 | state 构造时不传 sandbox | 不报错（NotRequired） |
| 7 | TitleMiddleware 二次触发 | 跳过（title 已存在） |
| 8 | ViewImageMiddleware 重复触发 | 跳过（已检查注入标记） |
| 9 | 中间件声明最小状态子集 | 与 ThreadState 兼容（结构化类型） |

## 依赖模块

| 模块 | 本模块调用的接口 |
|------|----------------|
| **无外部依赖** | 状态模式是底层定义，被其他所有模块依赖 |

---

## 参考源码

以下是已验证的源码实现。阅读并理解其模式，然后根据 `<ADAPT>` 适配参数在新项目中重写。
不要复制粘贴——重点关注每个函数的**决策分支**和**边界条件处理**。

### 文件清单（按阅读顺序）

| 文件 | 职责 | 阅读重点 |
|------|------|---------|
| `thread_state.py` | 状态定义 + reducer | TypedDict 继承 AgentState；`Annotated` reducer 标注；`NotRequired` 可选字段；`merge_artifacts` 去重保序；`merge_viewed_images` 空字典清空语义 |
| `thread_data_middleware.py` | 线程数据初始化 | `lazy_init` 延迟目录创建；`before_agent` 注入 `thread_data`；从 `runtime.context` 或 `get_config()` 获取 thread_id |
| `uploads_middleware.py` | 上传文件注入 | 从 `additional_kwargs.files` 提取新文件；扫描上传目录获取历史文件；`<uploaded_files>` 标签注入最后一条 human 消息 |
| `view_image_middleware.py` | 图片注入 LLM | `before_model` 检查上一轮 view_image 工具是否完成；base64 编码图片注入 HumanMessage；清空 viewed_images 防重复 |
| `title_middleware.py` | 自动标题生成 | `after_model` 检查 `state.get("title")` 是否已存在；用独立模型实例生成标题；失败时 fallback 到截断用户消息 |
| `clarification_middleware.py` | 澄清拦截 | `wrap_tool_call` 拦截 `ask_clarification`；返回 `Command(goto=END)` 中断执行；格式化澄清消息 |

源码文件见同目录下的 `src/` 子文件夹。
