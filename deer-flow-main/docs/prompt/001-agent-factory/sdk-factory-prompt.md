# 001a-SDK 层工厂（factory.py）

## 解决什么问题

`create_deerflow_agent` 是纯参数组装入口——接受 Python 对象，不读配置文件，不依赖全局单例。子智能体和外部集成直接调用它创建 Agent。

与上层 `make_lead_agent` 的分工：
- **SDK 层（本模块）**：纯参数 → Agent，无 I/O，无配置。可独立测试。
- **应用层（agent.py）**：读配置 + 请求参数 → 调 SDK 层或手动组装。业务策略只改这一层。
- 应用层当前**自己组装中间件**（`_build_middlewares`），不走 SDK 层的 features 路径。

## 职责边界

**只负责组装策略**：互斥验证、特性标志解析、中间件排序、工具去重。
不负责：配置加载、模型实例化、工具实现、中间件实现。

## 不可变的设计决策

### 互斥验证用 `is not None` 而非 truthy

```python
# 正确：空列表 [] 是合法的"完全接管模式"
if middleware is not None and features is not None:
    raise ValueError(...)

# 错误：[] 会被 falsy 放过
if middleware and features: ...
```

**Why**: `middleware=[]` 表示"接管中间件但为空"，是合法意图。truthy 检查会把它和 `None` 混淆。

### 三态特性标志：False / True / 实例

每个特性字段接受三种值：
- `False` — 跳过
- `True` — 使用内置默认中间件（部分特性无默认值，True 会 raise）
- `AgentMiddleware 实例` — 直接使用（自定义替换）

**无合理默认值的特性（summarization、guardrail），True 必须 raise 而非静默跳过。**

**Why**: Summarization 需要传 model 参数，Guardrail 需要传策略，框架无法猜出合理值。静默跳过比报错更难排查。

### 终端中间件不变量

无论怎么插入额外中间件，`ClarificationMiddleware` 必须在链尾。`@Next` 可能把它推离末位，插入后必须强制归位。

```python
clar_idx = next(i for i, m in enumerate(chain) if isinstance(m, ClarificationMiddleware))
if clar_idx != len(chain) - 1:
    chain.append(chain.pop(clar_idx))
```

**Why**: ClarificationMiddleware 通过 `Command(goto=END)` 中断执行流，后面有中间件的话不会被触发，导致澄清请求静默丢失。

### 工具去重用户优先

特性注入的工具（如 `view_image_tool`）和用户提供的工具按 name 去重，用户版本胜出。

```python
existing_names = {t.name for t in effective_tools}
for t in extra_tools:
    if t.name not in existing_names:
        effective_tools.append(t)
```

**Why**: 用户可能提供同名工具的自定义实现（如增强版 view_image），用户意图应覆盖框架默认。

### 声明式定位而非索引

`@Next/@Prev` 让调用方说"在谁旁边"而非"在位置几"。框架内部链顺序变化时不会静默错位。

**Why**: 索引定位脆弱——中间件增减后索引全部失效，且无编译期警告。

### 延迟导入避免循环依赖

工具和中间件在函数体内 `from deerflow.xxx import ...`，而非文件顶部 import。

**Why**: 顶层导入在 `agents` 和 `tools` 包之间形成循环引用。函数体内导入打破循环。

## 中间件组装顺序

`_assemble_from_features` 按 14 个固定位置构建链：

```
[0-2]  沙箱基础设施：ThreadData → Uploads → Sandbox
[3]    DanglingToolCallMiddleware（始终）
[4]    GuardrailMiddleware（特性：guardrail）
[5]    ToolErrorHandlingMiddleware（始终）
[6]    SummarizationMiddleware（特性：summarization）
[7]    TodoMiddleware（参数：plan_mode）
[8]    TitleMiddleware（特性：auto_title）
[9]    MemoryMiddleware（特性：memory）
[10]   ViewImageMiddleware（特性：vision）
[11]   SubagentLimitMiddleware（特性：subagent）
[12]   LoopDetectionMiddleware（始终）
[13]   ClarificationMiddleware（始终在最后）
```

两阶段排序：
1. 内置链 — 固定顺序追加
2. 额外中间件 — 通过 `@Next/@Prev` 插入，最后强制 ClarificationMiddleware 归位

## 适配层

```yaml
<ADAPT>
# === 框架 ===
framework: "langgraph"
create_agent_fn: "create_agent"
middleware_type: "AgentMiddleware"
state_schema: "ThreadState"

# === 入口函数 ===
sdk_factory_fn: "create_deerflow_agent"

# === 特性标志数据类 ===
features_class: "RuntimeFeatures"
# 三态字段：False / True / AgentMiddleware 实例
</ADAPT>
```

## 自检清单

| # | 验证 | 期望 | 代码位置 |
|---|------|------|---------|
| 1 | middleware + features 同时传 | ValueError | factory.py:107-108 |
| 2 | middleware + extra_middleware 同时传 | ValueError | factory.py:109-110 |
| 3 | extra_middleware 含非 AgentMiddleware | TypeError | factory.py:112-114 |
| 4 | 特性 True 但无内置默认（guardrail/summarization） | ValueError | factory.py:210, 220 |
| 5 | 用户工具与特性注入工具同名 | 用户版本胜出 | factory.py:130-133 |
| 6 | @Next 把终端中间件推离末位 | 强制归位 | factory.py:284-286 |
| 7 | 两个 @Next 同锚点 | ValueError 冲突 | factory.py:321 |
| 8 | @Next(A)+@Next(B) 循环 | ValueError | factory.py:366 |
| 9 | 两次调用不同参数 | 完全独立实例 | factory.py:136-144 |

## 依赖模块

| 模块 | 本模块调用的接口 |
|------|----------------|
| **框架层** | `create_agent()` 编译 Agent 图 |
| **特性标志** | `RuntimeFeatures` 数据类、`@Next/@Prev` 装饰器 |
| **中间件类** | 各具体中间件的构造函数（延迟导入） |
| **状态模式** | `ThreadState` 类定义 |
| **内置工具** | `ask_clarification_tool`, `view_image_tool`, `task_tool` |

---

## 函数编写策略详解

### create_deerflow_agent 参数说明

```python
def create_deerflow_agent(
    model: BaseChatModel,                              # 必填：聊天模型实例
    tools: list[BaseTool] | None = None,               # 用户提供的工具列表
    *,
    system_prompt: str | None = None,                  # 系统提示词，None 用最小默认
    middleware: list[AgentMiddleware] | None = None,    # 完全接管模式：直接使用此列表
    features: RuntimeFeatures | None = None,            # 声明式特性标志
    extra_middleware: list[AgentMiddleware] | None = None,  # 通过 @Next/@Prev 定位插入
    plan_mode: bool = False,                           # 启用 TodoMiddleware
    state_schema: type | None = None,                  # LangGraph 状态类型，默认 ThreadState
    checkpointer: BaseCheckpointSaver | None = None,   # 持久化后端
    name: str = "default",                             # 智能体名称（传递给 MemoryMiddleware 等）
) -> CompiledStateGraph:
```

**互斥规则**：`middleware`（完全接管）不能与 `features` 或 `extra_middleware` 组合。三条分支：

| 传入参数 | 行为 |
|---------|------|
| `middleware` 非 None | 直接使用此列表，跳过所有自动组装 |
| `features` 非 None（或 extra_middleware 非 None） | 从特性标志自动组装 + 插入 extra |
| 都不传 | 用空 `RuntimeFeatures()` 组装最小链 |

### 基于特性的中间件组装（_assemble_from_features）

**策略**：对每个特性字段，重复同一个三态判断模板：

```python
if feat.xxx is not False:                  # False → 跳过
    if isinstance(feat.xxx, AgentMiddleware):  # 实例 → 直接用
        chain.append(feat.xxx)
    else:                                       # True → 创建内置默认
        chain.append(XxxMiddleware())            # 无默认的会 raise ValueError
```

每个特性的处理细节：

| 位置 | 特性 | False | True | 实例 | 额外副作用 |
|------|------|-------|------|------|-----------|
| [0-2] sandbox | `feat.sandbox` | 跳过全部 3 个 | ThreadData + Uploads + Sandbox | 只用传入的那 1 个实例 | — |
| [3] DanglingToolCall | 始终 | — | — | — | 始终追加 |
| [4] Guardrail | `feat.guardrail` | 跳过 | **raise**（无内置默认） | 直接用 | — |
| [5] ToolErrorHandling | 始终 | — | — | — | 始终追加 |
| [6] Summarization | `feat.summarization` | 跳过 | **raise**（需要 model 参数） | 直接用 | — |
| [7] Todo | `plan_mode` 参数 | — | — | — | 仅 plan_mode=True 时追加 |
| [8] Auto Title | `feat.auto_title` | 跳过 | TitleMiddleware() | 直接用 | — |
| [9] Memory | `feat.memory` | 跳过 | MemoryMiddleware(agent_name=name) | 直接用 | — |
| [10] Vision | `feat.vision` | 跳过 | ViewImageMiddleware() | 直接用 | **额外注入 view_image_tool** |
| [11] Subagent | `feat.subagent` | 跳过 | SubagentLimitMiddleware() | 直接用 | **额外注入 task_tool** |
| [12] LoopDetection | 始终 | — | — | — | 始终追加 |
| [13] Clarification | 始终 | — | — | — | 始终追加 + **额外注入 ask_clarification_tool** |

**关键细节**：
- sandbox 特性控制 3 个中间件（ThreadData + Uploads + Sandbox），但传入实例时只替换为那 1 个实例
- vision 和 subagent 特性除了追加中间件，还会向 `extra_tools` 注入对应工具
- ClarificationMiddleware 始终追加，且附带 `ask_clarification_tool`
- 所有中间件在函数体内延迟导入（`from deerflow.agents.middlewares.xxx import XxxMiddleware`）

### @Next/@Prev 插入额外中间件（_insert_extra）

**策略**：声明式锚点定位 + 迭代解析，分四步：

**第一步：分类**

扫描 `extra_middleware` 中每个实例的类属性 `_next_anchor` / `_prev_anchor`（由 `@Next/@Prev` 装饰器设置），分为三类：

| 类别 | 条件 | 插入位置 |
|------|------|---------|
| anchored (next) | 有 `_next_anchor` | 锚点中间件**之后** |
| anchored (prev) | 有 `_prev_anchor` | 锚点中间件**之前** |
| unanchored | 无锚点 | ClarificationMiddleware **之前** |

**第二步：冲突检测**

```
两个 extra 同 @Next(X)          → ValueError 冲突
同 X 的 @Next 和 @Prev 冲突      → ValueError 冲突
同一个中间件同时有 @Next 和 @Prev → ValueError 不允许
```

**第三步：无锚点的插入**

直接插到 ClarificationMiddleware 之前（不破坏终端不变量）：

```python
clarification_idx = next(i for i, m in enumerate(chain) if isinstance(m, ClarificationMiddleware))
for mw in unanchored:
    chain.insert(clarification_idx, mw)
    clarification_idx += 1  # 后续无锚点的插在前面那个的后面
```

**第四步：有锚点的迭代解析**

支持跨 extra 锚定（extra A 锚定 extra B），需要多轮迭代：

```python
pending = list(anchored)
max_rounds = len(pending) + 1
for _ in range(max_rounds):
    if not pending:
        break
    remaining = []
    for mw, direction, anchor in pending:
        idx = next((i for i, m in enumerate(chain) if isinstance(m, anchor)), None)
        if idx is None:
            remaining.append(...)     # 锚点还没入链，等下一轮
            continue
        if direction == "next":
            chain.insert(idx + 1, mw)  # 锚点之后
        else:
            chain.insert(idx, mw)      # 锚点之前
    if len(remaining) == len(pending):
        # 一轮下来没有任何进展 → 锚点不在链中
        # 检查是否是 extra 之间的循环依赖
        circular = anchor_types & remaining_types
        if circular:
            raise ValueError("Circular dependency...")
        raise ValueError("Cannot resolve positions...")
    pending = remaining
```

**迭代终止条件**：
- `pending` 为空 → 全部解析成功
- 某轮 remaining 数量不变 → 锚点不存在或循环依赖，raise
- 超过 `len(pending) + 1` 轮 → 兜底退出

**第五步：终端不变量修复**

`@Next(ClarificationMiddleware)` 可能把 ClarificationMiddleware 推离末位。在 `_assemble_from_features` 中最后一步强制归位：

```python
clar_idx = next(i for i, m in enumerate(chain) if isinstance(m, ClarificationMiddleware))
if clar_idx != len(chain) - 1:
    chain.append(chain.pop(clar_idx))
```

### RuntimeFeatures 数据类

```python
@dataclass
class RuntimeFeatures:
    sandbox: bool | AgentMiddleware = True           # 沙箱环境（默认启用）
    memory: bool | AgentMiddleware = False           # 记忆系统（默认禁用）
    summarization: Literal[False] | AgentMiddleware = False  # 需自定义实例
    subagent: bool | AgentMiddleware = False         # 子智能体委托
    vision: bool | AgentMiddleware = False           # 视觉理解
    auto_title: bool | AgentMiddleware = False       # 自动标题
    guardrail: Literal[False] | AgentMiddleware = False      # 需自定义实例
```

**类型约束**：
- `summarization` 和 `guardrail` 用 `Literal[False]` 而非 `bool`，静态类型检查就能阻止 `summarization=True`
- 其他特性用 `bool | AgentMiddleware`，True 可以通过类型检查

### @Next/@Prev 装饰器

```python
def Next(anchor: type[AgentMiddleware]):
    """声明此中间件应放在 anchor 中间件之后。"""
    def decorator(cls):
        cls._next_anchor = anchor  # 在类上设置锚点标记
        return cls
    return decorator

def Prev(anchor: type[AgentMiddleware]):
    """声明此中间件应放在 anchor 中间件之前。"""
    def decorator(cls):
        cls._prev_anchor = anchor
        return cls
    return decorator
```

**使用示例**：

```python
@Next(LoopDetectionMiddleware)
class MyCustomMiddleware(AgentMiddleware):
    ...  # 会被插入到 LoopDetectionMiddleware 之后

# create_deerflow_agent 会把 MyCustomMiddleware 实例插入链中
create_deerflow_agent(
    model=model,
    features=RuntimeFeatures(),
    extra_middleware=[MyCustomMiddleware()],
)
```

---

## 参考源码

以下是已验证的源码实现。阅读并理解其模式，然后根据 `<ADAPT>` 适配参数在新项目中重写。
不要复制粘贴——重点关注每个函数的**决策分支**和**边界条件处理**。

### 文件清单（按阅读顺序）

| 文件 | 职责 | 阅读重点 |
|------|------|---------|
| `agents/features.py` | 特性标志 + @Next/@Prev 装饰器 | 三态字段的类型声明方式；装饰器如何在类上设置 `_next_anchor` |
| `agents/factory.py` | SDK 层工厂 | `create_deerflow_agent` 的互斥验证；`_assemble_from_features` 中三态模板的重复应用；`_insert_extra` 的迭代解析算法 |

源码文件见同目录下的 `src/` 子文件夹。
