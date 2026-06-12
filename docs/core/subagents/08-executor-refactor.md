# 08 - Executor 重构演进：从基础执行到企业级子代理引擎

> 本文档追踪 `subagents/executor.py` 从初始实现到当前的 15+ 次重构，聚焦每个重构解决的具体问题、架构决策和它们如何叠加成当前的执行引擎形态。

---

## 一、当前架构全貌

```
┌──────────────────────────────────────────────────────────────────────┐
│                        SubagentExecutor 架构                          │
│                                                                      │
│  初始化 (__init__)                                                    │
│  ├── SubagentConfig → 模型解析(resolve_subagent_model_name)           │
│  ├── 工具过滤(_filter_tools: allowlist + denylist)                    │
│  └── trace_id 传播（分布式追踪）                                       │
│                                                                      │
│  构建阶段 (_build_initial_state)                                      │
│  ├── 加载技能(_load_skills) → asyncio.to_thread 避免阻塞              │
│  ├── 技能工具策略过滤(filter_tools_by_skill_allowed_tools)             │
│  ├── 组装延迟工具(assemble_deferred_tools) — MCP schema 按需加载       │
│  ├── 合并 system_prompt + skills + deferred → 单条 SystemMessage      │
│  └── 返回 (state, final_tools, deferred_setup)                        │
│                                                                      │
│  执行阶段 (_aexecute)                                                 │
│  ├── 创建 Agent(_create_agent) → 中间件栈 + deferred filter            │
│  ├── SubagentTokenCollector → LLM 用量收集                             │
│  ├── agent.astream() → 流式迭代                                       │
│  │   ├── 协作式取消检查(cancel_event.is_set())                         │
│  │   └── AI 消息捕获(去重 by message.id)                               │
│  └── try_set_terminal() → 原子终态转换                                 │
│                                                                      │
│  调度入口                                                             │
│  ├── execute() — 同步 API                                             │
│  │   ├── 检测运行中事件循环 → _execute_in_isolated_loop                │
│  │   └── 无事件循环 → asyncio.run()                                   │
│  └── execute_async() — 后台任务 API                                   │
│      └── _scheduler_pool → 持久化事件循环 → _aexecute                  │
│                                                                      │
│  全局状态                                                             │
│  ├── _background_tasks: dict[task_id → SubagentResult]                │
│  ├── _scheduler_pool: ThreadPoolExecutor(3 workers)                   │
│  └── _isolated_subagent_loop: 持久化事件循环(daemon thread)            │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 二、重构时间线

按时间顺序，每个重构解决一个具体的生产问题：

### 1. 事件循环冲突修复 (#1965)

**问题**：子代理 `execute()` 是同步 API，直接 `asyncio.run()` 在已有事件循环的线程里会 crash。

**修复**：检测 `asyncio.get_running_loop()` 是否存在，是则走持久化事件循环路径。

```python
def execute(self, task, result_holder=None):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        return self._execute_in_isolated_loop(task, result_holder)
    return asyncio.run(self._aexecute(task, result_holder))
```

**引入的组件**：
- `_isolated_subagent_loop` — 守护线程中的长生命周期 `asyncio.AbstractEventLoop`
- `_get_isolated_subagent_loop()` — 懒初始化 + 崩溃恢复
- `_submit_to_isolated_loop_in_context()` — 保持 ContextVar 传递

### 2. 协作式取消 (#1873)

**问题**：子代理在线程池中执行，无法被 `Future.cancel()` 强制终止（Python 线程不可中断）。

**修复**：引入 `cancel_event: threading.Event`，在 `astream()` 迭代边界检查。

```python
# 请求取消方
def request_cancel_background_task(task_id):
    result.cancel_event.set()

# 执行方检查
async for chunk in agent.astream(...):
    if result.cancel_event.is_set():
        result.try_set_terminal(CANCELLED, error="Cancelled by user")
        return result
```

**设计权衡**：取消只在 `astream` 迭代边界生效——长工具调用期间无法中断，但保证了状态一致性。

### 3. 结构化内容序列化 (#1215)

**问题**：LLM 返回的 content 可能是 `str` 也可能是 `list[dict]`（多模态），需要统一提取文本。

**修复**：`_aexecute` 中的结果提取逻辑处理三种格式：

```
content: str        → 直接使用
content: list[dict] → 提取 text 字段，拼接
content: 其他       → str() 兜底
```

### 4. 每个子代理加载技能 (#2253)

**问题**：子代理无法使用技能系统，功能受限。

**修复**：新增 `_load_skills()`、`_load_skill_messages()`、`_apply_skill_allowed_tools()` 三个方法。

**技能加载流程**：
```
_load_skills()
  ├── asyncio.to_thread(get_or_new_skill_storage)
  ├── asyncio.to_thread(storage.load_skills, enabled_only=True)
  └── config.skills 白名单过滤

_load_skill_messages(skills)
  ├── asyncio.to_thread(skill.skill_file.read_text)
  └── 返回 [SystemMessage(content='<skill name="xxx">...')]
```

技能内容作为 conversation item 注入（而非 system prompt），与 Codex 模式对齐。

### 5. AppConfig 穿透 (#2666, #2652)

**问题**：子代理每次需要配置时都调用 `get_app_config()` 全局单例，测试困难且与 lead agent 模式不一致。

**修复**：`SubagentExecutor.__init__` 接受 `app_config` 参数，显式穿透到所有子组件。

```python
def __init__(self, config, tools, app_config=None, ...):
    self.app_config = app_config
    # 模型解析：有 app_config 就用它，没有才 fallback 到 get_app_config()
    if config.model != "inherit" or parent_model is not None or app_config is not None:
        self.model_name = resolve_subagent_model_name(config, parent_model, app_config=app_config)
    else:
        self.model_name = None  # 延迟到 _create_agent 时解析
```

**设计**：模型名解析是两阶段的——构造时尽力解析，`_create_agent` 兜底。让没有 config 文件的单元测试也能构造 executor。

### 6. 模型覆盖影响工具和中间件 (#2641)

**问题**：子代理的模型覆盖（如 bash 用便宜模型）没有传递到中间件工厂，导致中间件使用了错误的模型配置。

**修复**：`_create_agent` 调用 `build_subagent_runtime_middlewares` 时传入 `model_name`，中间件栈根据模型能力决定行为（如是否加载 ViewImageMiddleware）。

### 7. 用户上下文跨线程传播 (#2676)

**问题**：`execute_async` 在 `_scheduler_pool` 的新线程中执行，`ContextVar`（如用户身份）丢失。

**修复**：`copy_context()` 在主线程快照上下文，通过 `_submit_to_isolated_loop_in_context` 传递。

```python
parent_context = copy_context()

def run_task():
    # 在 _scheduler_pool 线程中
    execution_future = _submit_to_isolated_loop_in_context(
        parent_context,  # ← 携带原始 ContextVar
        lambda: self._aexecute(task, result_holder),
    )
```

### 8. SystemMessage 合并 (#2701)

**问题**：部分 LLM API（vLLM、Xinference、国产大模型）拒绝多条 SystemMessage："System message must be at the beginning."

**修复**：将 `system_prompt` + 所有 skill 内容合并为**一条** `SystemMessage`，`create_agent()` 传 `system_prompt=None`。

```python
system_parts: list[str] = []
if self.config.system_prompt:
    system_parts.append(self.config.system_prompt)
for skill_msg in skill_messages:
    system_parts.append(skill_msg.content)
# deferred section 也合并进来
deferred_section = get_deferred_tools_prompt_section(deferred_names=...)
if deferred_section:
    system_parts.append(deferred_section)

messages.append(SystemMessage(content="\n\n".join(system_parts)))
```

### 9. 终态原子转换 (#2583)

**问题**：超时线程和执行线程竞态写入 `SubagentResult.status`——超时设了 `TIMED_OUT`，执行随后又覆盖为 `COMPLETED`。

**修复**：引入 `_state_lock` + `try_set_terminal()`，第一次终态转换赢，后续写入被拒绝。

```python
def try_set_terminal(self, status, *, result=None, error=None, ...):
    if not status.is_terminal:
        raise ValueError(f"Status {status} is not terminal")
    with self._state_lock:
        if self.status.is_terminal:
            return False  # 已终态，拒绝覆盖
        # ... 设置字段 ...
        self.status = status
        return True
```

**关键保证**：超时和取消线程只能通过 `try_set_terminal` 转换状态，不会与执行线程产生数据竞争。

### 10. Token 用量合并到父代理 (#2838)

**问题**：子代理的 LLM token 用量独立于父代理，无法统计真实成本。

**修复**：`SubagentTokenCollector` 作为 LangChain 回调收集每次 LLM 调用的 `usage_metadata`，执行完成后存入 `SubagentResult.token_usage_records`。

```
_aexecute()
  ├── collector = SubagentTokenCollector(caller="subagent:bash")
  ├── RunnableConfig(callbacks=[collector])
  ├── agent.astream() 期间 collector.on_llm_end() 收集
  └── result.try_set_terminal(token_usage_records=collector.snapshot_records())
```

### 11. 技能工具策略强制 (#2626)

**问题**：`config.tools` 白名单可以引用技能禁用的工具，绕过技能级工具限制。

**修复**：`_build_initial_state` 中先加载技能，再用 `filter_tools_by_skill_allowed_tools` 过滤，**之后**才组装延迟工具。顺序保证：技能策略 > 延迟工具 > 名称级策略。

```
_load_skills() → _apply_skill_allowed_tools(skills) → assemble_deferred_tools(filtered_tools)
```

### 12. 延迟 MCP 工具加载扩展到子代理 (#3432)

**问题**：lead agent 有延迟工具加载（MCP schema 按需加载），子代理没有——大量 MCP 工具时浪费 token 且可能导致模型混淆。

**修复**：子代理复用 lead agent 的延迟工具路径。

```
_build_initial_state():
  ├── filtered_tools = skill_policy(base_tools)
  ├── final_tools, deferred_setup = assemble_deferred_tools(filtered_tools)
  └── deferred_section 写入 system prompt

_create_agent():
  └── build_subagent_runtime_middlewares(deferred_setup=deferred_setup)
      └── DeferredToolFilterMiddleware(deferred_names, catalog_hash)
```

**工具层级**：
```
base_tools (原始)
  → config.tools allowlist 过滤
    → config.disallowed_tools denylist 过滤
      → skill allowed_tools 策略过滤
        → assemble_deferred_tools (MCP 延迟)
          → final_tools (最终给模型的)
```

---

## 三、中间件栈对比

### Lead Agent 中间件栈

```
build_lead_runtime_middlewares()
  ├── ToolErrorHandlingMiddleware
  ├── ContextCompressionMiddleware
  ├── HumanInTheLoopMiddleware
  ├── UploadMiddleware (include_uploads=True)
  ├── DanglingToolCallPatchMiddleware
  ├── DeferredToolFilterMiddleware (如有延迟工具)
  └── SafetyFinishReasonMiddleware (如启用)
```

### Subagent 中间件栈

```
build_subagent_runtime_middlewares()
  ├── ToolErrorHandlingMiddleware
  ├── ContextCompressionMiddleware
  ├── HumanInTheLoopMiddleware
  ├── (无 UploadMiddleware — include_uploads=False)
  ├── DanglingToolCallPatchMiddleware
  ├── DeferredToolFilterMiddleware (如有延迟工具)
  ├── ViewImageMiddleware (模型支持视觉时)
  └── SafetyFinishReasonMiddleware (如启用)
```

**关键差异**：
- 子代理**没有 UploadMiddleware**——子代理不处理文件上传
- 子代理**有条件加载 ViewImageMiddleware**——根据模型能力决定
- 其余共享同一套中间件，通过 `build_subagent_runtime_middlewares` 统一组装

---

## 四、执行路径选择

```
execute(task, result_holder)
  │
  ├── 检测 asyncio.get_running_loop()
  │   ├── 有运行中的循环 → _execute_in_isolated_loop()
  │   │   └── _submit_to_isolated_loop_in_context(copy_context(), coro)
  │   │       └── _get_isolated_subagent_loop().run_coroutine_threadsafe()
  │   │           └── future.result(timeout=timeout_seconds)
  │   │               └── FuturesTimeoutError → cancel_event.set()
  │   │
  │   └── 无循环 → asyncio.run(_aexecute())
  │
  └── 异常兜底 → try_set_terminal(FAILED)

execute_async(task, task_id)
  │
  ├── SubagentResult(PENDING) → _background_tasks[task_id]
  │
  └── _scheduler_pool.submit(run_task)
      ├── status → RUNNING
      ├── _submit_to_isolated_loop_in_context(copy_context(), coro)
      ├── future.result(timeout=timeout_seconds)
      │   └── FuturesTimeoutError → cancel_event + try_set_terminal(TIMED_OUT)
      └── Exception → try_set_terminal(FAILED)
```

**三层超时保护**：
1. `future.result(timeout=...)` — 等待执行完成的总超时
2. `cancel_event.set()` — 协作式信号，要求执行在下一个 `astream` 迭代边界停止
3. `try_set_terminal(TIMED_OUT)` — 原子终态转换，防止执行线程后到覆盖

---

## 五、线程安全模型

### 共享状态

| 状态 | 保护机制 | 访问者 |
|------|---------|--------|
| `SubagentResult.status` | `_state_lock` | 执行线程、超时线程、取消线程 |
| `_background_tasks` | `_background_tasks_lock` | 调度线程、轮询线程 |
| `_isolated_subagent_loop` | `_isolated_subagent_loop_lock` | 任意调用线程 |

### 竞态场景

**场景 1：超时 vs 完成**
```
时间线:
  T1: 超时线程 → cancel_event.set()
  T2: 超时线程 → try_set_terminal(TIMED_OUT) → 成功，status=TIMED_OUT
  T3: 执行线程 → astream 完成 → try_set_terminal(COMPLETED) → 失败，已被终态化
```

**场景 2：取消 vs 执行**
```
时间线:
  T1: 用户取消 → cancel_event.set()
  T2: 执行线程 → 下一个 astream 迭代 → 检查 cancel_event → try_set_terminal(CANCELLED)
```

**场景 3：孤立事件循环崩溃恢复**
```
时间线:
  T1: loop 线程崩溃 → loop 关闭
  T2: 下一个 execute → _get_isolated_subagent_loop() → 检测 loop 不可用
  T3: 创建新 loop + 新守护线程 → 恢复
```

---

## 六、设计模式总结

| 模式 | 体现 | 解决的问题 |
|------|------|-----------|
| **持久化事件循环** | `_isolated_subagent_loop` | 避免每次执行创建/关闭循环，复用 httpx 连接池 |
| **协作式取消** | `cancel_event` + astream 迭代检查 | Python 线程不可强制终止，用信号通知 |
| **原子终态转换** | `try_set_terminal` + `_state_lock` | 超时/取消/执行三方竞态写入 |
| **两阶段模型解析** | `__init__` 尽力 + `_create_agent` 兜底 | 测试环境无 config 文件也能构造 |
| **ContextVar 快照** | `copy_context()` 穿透线程池 | 用户身份等上下文不丢失 |
| **单条 SystemMessage** | system_prompt + skills + deferred 合并 | 兼容 vLLM 等国产大模型 API |
| **延迟工具加载** | `assemble_deferred_tools` → `DeferredToolFilterMiddleware` | MCP 工具按需加载，减少 token 消耗 |
| **技能工具策略级联** | skill policy → name policy → deferred | 工具过滤的优先级保证 |

---

## 七、重构文件索引

| 重构 | PR | 改动文件 |
|------|-----|---------|
| 事件循环冲突 | #1965 | executor.py |
| 协作式取消 | #1873 | executor.py |
| 结构化内容序列化 | #1215 | executor.py |
| 技能加载 | #2253 | executor.py, config.py |
| AppConfig 穿透 | #2666, #2652 | executor.py, tool_error_handling_middleware.py |
| 模型覆盖 | #2641 | executor.py |
| 上下文传播 | #2676 | executor.py |
| SystemMessage 合并 | #2701 | executor.py |
| 终态原子转换 | #2583 | executor.py |
| Token 用量收集 | #2838 | executor.py, token_collector.py |
| 技能工具策略 | #2626 | executor.py |
| 延迟 MCP 加载 | #3432 | executor.py, tool_search.py, tool_error_handling_middleware.py |
