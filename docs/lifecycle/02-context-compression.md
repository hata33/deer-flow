# 上下文压缩全链路

> 当对话 token 接近上限时，系统如何自动压缩上下文——从触发检测到跨模块保护（memory flush、todo rescue、dynamic context 保护），再到摘要生成和前端隐藏的完整跨模块协作路径。

---

## 全链路架构图

```
用户消息进入
     │
     ▼
┌─ Config 加载 ─────────────────────────────────────┐
│ config.yaml → SummarizationConfig                  │
│ (enabled, model_name, trigger, keep, preserve)     │
└────────────────────────┬───────────────────────────┘
                         │
                         ▼
┌─ 中间件注册 ─────────────────────────────────────┐
│ _build_middlewares() (agent.py:319-324)           │
│ └─ _create_summarization_middleware()             │
│     └─ 挂载 memory_flush_hook 为 BeforeHook      │
└────────────────────────┬───────────────────────────┘
                         │
                         ▼ (每次 before_model)
┌─ 触发检测 ───────────────────────────────────────┐
│ _should_summarize()                               │
│ ├─ 计算 token 总量（token_counter）               │
│ ├─ 检查是否超过 trigger 阈值                       │
│ └─ 未超阈值 → 跳过，继续正常流程                    │
└────────────────────────┬───────────────────────────┘
                         │ (超阈值)
                         ▼
┌─ 跨模块协调 ─────────────────────────────────────┐
│                                                    │
│  ① Memory: memory_flush_hook()                    │
│     └─ 消息被删除前 → 立即排队给记忆系统            │
│         (add_nowait，跳过 debounce)                 │
│                                                    │
│  ② DynamicContext: _preserve_dynamic_context()    │
│     └─ 从 messages_to_summarize 中移出 reminder    │
│         防止日期/记忆注入被误删                      │
│                                                    │
│  ③ Skills: _partition_with_skill_rescue()          │
│     └─ 保留最近 N 个技能文件读取的 ToolMessage      │
│         不超过 token 预算                           │
│                                                    │
│  ④ Todo: (检测在下一轮 before_model)               │
│     └─ write_todos 被摘要截断后 → 注入提醒消息      │
└────────────────────────┬───────────────────────────┘
                         │
                         ▼
┌─ 摘要生成 ───────────────────────────────────────┐
│ LLM 调用（独立模型，标签 middleware:summarize）     │
│ ├─ 使用 config.model_name 或默认轻量模型           │
│ ├─ 输入: 被分区前的消息列表                        │
│ └─ 输出: 摘要文本                                  │
│                                                    │
│ 结果格式:                                          │
│ HumanMessage(                                      │
│   content="Here is a summary of ...",              │
│   name="summary"     ← 前端识别此标记隐藏显示      │
│ )                                                  │
└────────────────────────┬───────────────────────────┘
                         │
                         ▼
┌─ 消息重建 ───────────────────────────────────────┐
│ _build_new_messages()                              │
│ ├─ 摘要消息（name="summary"）                      │
│ ├─ 保留的动态上下文 reminder                        │
│ ├─ 保留的技能 ToolMessage                           │
│ └─ cutoff 之后的消息（原样保留）                    │
└────────────────────────┬───────────────────────────┘
                         │
                         ▼
┌─ 前端处理 ───────────────────────────────────────┐
│ hooks.ts:                                          │
│ if (m.name === "summary" && m.type === "human")   │
│   → summarizedRef.add(m.id)  // 对用户隐藏         │
│   → 但模型仍可见，作为上下文继续对话                 │
└────────────────────────────────────────────────────┘
```

---

## 步骤 ①：配置加载

**文件**: `deerflow/config/summarization_config.py`

```yaml
# config.yaml
summarization:
  enabled: true
  model_name: null                    # null = 使用轻量模型
  trigger:                            # 触发条件（满足任一即触发）
    - tokens: 100000                  # token 数超过阈值
    - messages: 100                   # 消息数超过阈值
  keep: 20                            # 保留最近 20 条消息
  preserve_recent_skill_count: 5      # 保护最近 5 个技能 bundle
  preserve_recent_skill_tokens: 25000 # 技能保护 token 预算
```

加载路径：`config.yaml → AppConfig → _apply_singleton_configs() → load_summarization_config_from_dict()`

---

## 步骤 ②：中间件注册

**文件**: `deerflow/agents/lead_agent/agent.py` → `_build_middlewares()` (line 319-324)

```python
summarization_middleware = _create_summarization_middleware(app_config=resolved_app_config)
if summarization_middleware is not None:
    middlewares.append(summarization_middleware)
```

**工厂函数** `_create_summarization_middleware()` (lines 102-163):
1. 读取 `SummarizationConfig`
2. 如果 `enabled=false` → 返回 None（不注册）
3. 创建独立 LLM 模型（`model_name` 或默认轻量模型）
4. 注册 `memory_flush_hook` 为 BeforeSummarizationHook
5. 标签：`["middleware:summarize"]` 用于 RunJournal 追踪

---

## 步骤 ③：触发检测

**文件**: `deerflow/agents/middlewares/summarization_middleware.py` → `_maybe_summarize()` (line 126)

**每次 before_model 钩子触发时执行**：

```
_maybe_summarize(messages)
  ├─ total_tokens = self.token_counter(messages)
  ├─ if not _should_summarize(total_tokens, len(messages)):
  │     └─ return messages  # 未超阈值，跳过
  ├─ cutoff_index = _determine_cutoff_index(messages)
  │     └─ 根据 keep 配置确定分割点
  ├─ 分区消息（多阶段）:
  │     ① 基础分区: _partition_messages() → messages_to_summarize + preserved
  │     ② 技能救援: _partition_with_skill_rescue() → 保留技能相关 ToolMessage
  │     ③ Reminder 保护: _preserve_dynamic_context_reminders() → 保留 system-reminder
  ├─ 跨模块保护处理（见步骤 ④）
  ├─ 生成摘要（见步骤 ⑤）
  └─ 重建消息（见步骤 ⑥）
```

---

## 步骤 ④：跨模块保护（核心协作）

这是上下文压缩中最复杂的部分——多个模块需要在消息被删除前保护各自的数据。

### 4.1 Memory ↔ Summarization: memory_flush_hook

**文件**: `deerflow/agents/memory/summarization_hook.py` → `memory_flush_hook()`

```
问题: 摘要会删除旧消息，但记忆系统需要从这些消息中提取信息
解决: before_summarization 钩子在删除前触发

memory_flush_hook(event: SummarizationEvent)
  ├─ filter_messages_for_memory(event.messages_to_summarize)
  │     └─ 去除工具调用、上传文件块，只保留用户+AI消息
  ├─ 检测纠正/强化信号
  ├─ 捕获 user_id（Timer 线程中 ContextVar 不可用，必须此时捕获）
  └─ queue.add_nowait(...)  ← 跳过 debounce，立即处理
```

**为什么用 add_nowait 而非 add**：消息即将被永久删除，不能再等 30 秒 debounce。

### 4.2 DynamicContext ↔ Summarization: reminder 保护

**文件**: `summarization_middleware.py` → `_preserve_dynamic_context_reminders()`

```
问题: DynamicContextMiddleware 注入的日期/记忆 reminder 可能在 cutoff 之前被删除
解决: 从 messages_to_summarize 中识别并移出 reminder 消息

识别方式:
  msg.additional_kwargs.get("dynamic_context_reminder") == True

  （不使用内容子串匹配，防止用户消息中恰好包含 <system-reminder> 被误判）

保护方式:
  将 reminder 从 "要摘要的消息" 移到 "保留的消息" 列表
```

### 4.3 Skills ↔ Summarization: 技能 bundle 保护

**文件**: `summarization_middleware.py` → `_partition_with_skill_rescue()` (lines 203-249)

```
问题: Agent 读取的技能文件内容（ToolMessage）可能在 cutoff 前被删除
      导致 Agent 失去对技能工作流的理解
解决: 识别并保留最近的技能文件读取

流程:
  ├─ _find_skill_bundles(messages)
  │     └─ 识别包含 /mnt/skills/ 路径的 ToolMessage（技能文件读取）
  ├─ _select_bundles_to_rescue(bundles)
  │     └─ 选择最近的 N 个（preserve_recent_skill_count）
  │         总 token 不超过 preserve_recent_skill_tokens
  └─ 将选中的 ToolMessage 从 "要摘要" 移到 "保留"
```

### 4.4 Todo ↔ Summarization: 上下文丢失检测

**文件**: `deerflow/agents/middlewares/todo_middleware.py` → `before_model` 钩子

```
问题: write_todos 工具调用被摘要截断后，Agent 不再知道自己有哪些任务
解决: 检测到上下文丢失后注入提醒

检测: _todos_in_messages() 扫描当前消息中是否有 write_todos 调用
处理: 如果任务状态存在但 write_todos 不在消息中 → 注入包含完整任务列表的提醒
限制: 最大提醒 2 次，防止无限循环
隐藏: 提醒消息 hide_from_ui=true，不显示给用户
```

---

## 步骤 ⑤：摘要生成

**文件**: `summarization_middleware.py` → `_create_summary()`

```
_create_summary(messages_to_summarize)
  ├─ 将消息格式化为文本
  ├─ 调用 LLM（独立模型实例，非对话主模型）
  │     └─ 标签: ["middleware:summarize"]
  │         日志可区分对话调用和摘要调用
  └─ 返回摘要文本
```

**模型选择**：
- `config.model_name` 指定时 → 使用指定模型
- 未指定时 → 使用默认轻量模型（节省成本）

---

## 步骤 ⑥：消息重建

**文件**: `summarization_middleware.py` → `_build_new_messages()` (lines 179-183)

```
最终消息列表 = [
  HumanMessage(name="summary", content="Here is a summary of ..."),  # 摘要
  ... dynamic context reminders ...,                                  # 保护的日期/记忆注入
  ... rescued skill ToolMessages ...,                                 # 保护的技能文件
  ... messages after cutoff ...,                                      # cutoff 之后的消息
]
```

关键特性：
- **name="summary"** 标记 → 前端识别并隐藏
- **模型可见** → 摘要作为上下文参与后续对话
- **消息 ID 不变** → LangGraph 的 add_messages reducer 正确替换

---

## 步骤 ⑦：前端处理

**文件**: `frontend/src/core/threads/hooks.ts` (lines 256-258)

```typescript
if (m.name === "summary" && m.type === "human") {
  summarizedRef.current?.add(m.id ?? "");
}
```

- `summarizedRef` 收集所有 summary 消息的 ID
- 渲染时跳过这些消息 → 用户看到的是无缝的对话（但上下文已被压缩）
- 如果用户问"我们之前聊了什么"，Agent 可以基于摘要回答

---

## 跨模块协作汇总

```
                    ┌─────────────────┐
                    │ Summarization   │
                    │ Middleware      │
                    └────────┬────────┘
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                   ▼
   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
   │ Memory       │  │ DynamicCtx   │  │ Skills       │
   │ Middleware   │  │ Middleware   │  │ Middleware   │
   │              │  │              │  │              │
   │ flush_hook:  │  │ 保护:        │  │ 保护:        │
   │ 删除前提取   │  │ 日期reminder │  │ 技能bundle   │
   │ → add_nowait │  │ 不被摘要     │  │ 不被摘要     │
   └──────────────┘  └──────────────┘  └──────────────┘
          │                                     │
          ▼                                     ▼
   ┌──────────────┐                    ┌──────────────┐
   │ Todo         │                    │ Frontend     │
   │ Middleware   │                    │              │
   │              │                    │ 隐藏:        │
   │ 检测:        │                    │ name=summary │
   │ 任务丢失     │                    │ 的消息       │
   │ → 注入提醒   │                    └──────────────┘
   └──────────────┘
```

---

## 深入阅读

| 模块 | 文档 |
|------|------|
| 中间件详解 | [docs/core/agent/05-middlewares.md](../core/agent/05-middlewares.md) |
| 记忆系统 | [docs/core/memory/01-memory-system.md](../core/memory/01-memory-system.md) |
| Agent 生命周期 | [docs/core/agent/02-lifecycle.md](../core/agent/02-lifecycle.md) |
| 配置系统 | [docs/core/config/04-feature-config.md](../core/config/04-feature-config.md) |
