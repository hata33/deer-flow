# 记忆功能上下文链路

> 记忆如何从存储到注入到更新形成闭环——一个完整的跨模块协作路径：存储加载 → DynamicContext 注入 → 对话交互 → MemoryMiddleware 触发 → Debounce 排队 → LLM 提取 → 原子保存 → 下次注入。

---

## 全链路架构图

```
                    ┌─────────────────────────────────────────────────────────┐
                    │              记忆功能上下文闭环                           │
                    └─────────────────────────────────────────────────────────┘
                                      │
    ┌─────────────────────────────────┼────────────────────────────────────┐
    │                                 │                                    │
    ▼                                 ▼                                    ▼
┌────────┐  读取  ┌──────────┐  注入  ┌──────────┐  对话  ┌──────────┐  触发 ┌───────────┐
│ Storage│ ────▸ │ Dynamic   │ ────▸ │ Agent    │ ────▸  │ Memory   │ ────▸│ Queue      │
│ (.json)│       │ Context   │       │ 对话执行  │        │Middleware│      │ (debounce)│
└────────┘       │ Middleware│       └──────────┘        └──────────┘      └───────────┘
     ▲           └──────────┘                                              │
     │                    ▲                                                ▼
     │                    │                                         ┌──────────┐
     │                    │ 注入                                    │ Updater  │
     │                    │                                         │ (LLM)    │
     │                    │                                         └──────────┘
     │                    │                                              │
     │                    │                                              ▼
     │              ┌──────────┐                                    ┌──────────┐
     │              │ 下次请求  │ ◂──────────────────────────────── │ 原子保存  │
     │              │ 注入更新  │                                    └──────────┘
     └──────────────┴──────────┘
```

---

## 步骤 ①：存储层

**文件**: `deerflow/agents/memory/storage.py`

### 数据结构 (memory.json)

```json
{
  "version": "1.0",
  "lastUpdated": "2026-05-26T10:30:00Z",
  "user": {
    "workContext":    {"summary": "...", "updatedAt": "..."},
    "personalContext": {"summary": "...", "updatedAt": "..."},
    "topOfMind":      {"summary": "...", "updatedAt": "..."}
  },
  "history": {
    "recentMonths":      {"summary": "..."},
    "earlierContext":    {"summary": "..."},
    "longTermBackground":{"summary": "..."}
  },
  "facts": [
    {
      "id": "fact_xxxxxxxx",
      "content": "用户偏好深色主题",
      "category": "preference",
      "confidence": 0.95,
      "createdAt": "2026-05-26T10:30:00Z",
      "source": "thread_id"
    }
  ]
}
```

### Per-User 隔离

```
{base_dir}/
├── users/
│   ├── user1/
│   │   ├── memory.json                 # 用户 1 的记忆
│   │   └── agents/custom/memory.json   # 用户 1 的自定义 Agent 记忆
│   └── default/                        # 无认证模式的回退用户
│       └── memory.json
```

### 缓存策略

```
_mtime_cache: { cache_key: (data, mtime) }
  └─ 每次读取前检查文件 mtime
     ├─ mtime 未变 → 返回缓存
     └─ mtime 已变 → 重新读取
```

---

## 步骤 ②：注入层（DynamicContextMiddleware）

**文件**: `deerflow/agents/middlewares/dynamic_context_middleware.py`

### 注入时机

| 条件 | 动作 |
|------|------|
| 首轮（last_date is None） | 注入完整 reminder（记忆 + 日期） |
| 同日（last_date == today） | 不注入 |
| 跨日（last_date ≠ today） | 注入轻量日期更新 |

### ID-Swap 技术

这是注入层的关键设计——在不破坏消息历史的前提下插入系统内容：

```
原始消息:
  HumanMessage(id="msg_001", content="帮我写个函数")

注入后:
  HumanMessage(id="msg_001", content="<system-reminder>...",  ← 取原消息 ID
               additional_kwargs={hide_from_ui: True,          ← 前端隐藏
                                  dynamic_context_reminder: True})
  HumanMessage(id="msg_001__user", content="帮我写个函数")     ← 派生 ID
```

**为什么用 ID-Swap**：LangGraph 的 `add_messages` reducer 按消息 ID 替换。通过让 reminder 取原消息 ID，实现"替换而非追加"，保持消息顺序正确。

### 注入格式

```xml
<system-reminder>
<memory>
  <context>
    <workContext>用户是全栈工程师，偏好 TypeScript</workContext>
    <topOfMind>正在重构认证模块</topOfMind>
  </context>
  <facts>
    - 用户偏好深色主题（偏好，95%）
    - 项目使用 Next.js 14（知识，90%）
  </facts>
</memory>
<current_date>2026-05-26, Tuesday</current_date>
</system-reminder>
```

**为什么注入到 HumanMessage 而非系统提示词**：系统提示词保持完全静态以最大化前缀缓存命中率。用户相关内容通过此中间件动态注入。

---

## 步骤 ③：对话层

用户与 Agent 正常交互，中间件链完整执行（参见 [01-agent-request-flow.md](01-agent-request-flow.md)）。

Agent 的回复基于注入的记忆上下文，用户感知不到记忆的存在。

---

## 步骤 ④：触发层（MemoryMiddleware）

**文件**: `deerflow/agents/middlewares/memory_middleware.py` → `after_agent()` 钩子

```
after_agent(state, runtime)
  ├─ 1. 从 runtime.context 获取 thread_id
  ├─ 2. filter_messages_for_memory(messages)
  │     ├─ 去除 AI 消息中的工具调用（只保留最终文本响应）
  │     ├─ 去除上传文件块（<uploaded_files> 标签）
  │     └─ 去除纯上传消息对应的 AI 回复
  ├─ 3. 检测信号
  │     ├─ 纠正信号: 11 种模式（"不对"、"我说的不是"、"不要这样" 等）
  │     └─ 强化信号: 13 种模式（"对"、"就是这样"、"很好" 等）
  │         优先级: 纠正 > 强化
  ├─ 4. 捕获 user_id（必须在此时捕获，Timer 线程中 ContextVar 不可用）
  └─ 5. queue.add(thread_id, messages, user_id, signals)
```

**为什么在 after_agent 而非 after_model**：需要等整个 Agent 交互完成（包括所有工具调用）后才处理，确保拿到最终响应。

---

## 步骤 ⑤：排队层（Debounce Queue）

**文件**: `deerflow/agents/memory/queue.py`

```
queue.add(thread_id, messages, ...)
  ├─ with self._lock:
  │     ├─ 检查是否已有相同 thread_id 的待处理项
  │     │     └─ 有 → 替换为新消息（去重）
  │     └─ 加入队列
  └─ _reset_timer() → 重置 debounce 计时器（30 秒）
       └─ 30 秒内无新消息 → 触发 _process_batch()

queue.add_nowait(thread_id, messages, ...)  ← memory_flush_hook 专用
  └─ 立即触发处理，跳过 debounce
       （消息即将被摘要删除，不能再等）
```

**去重策略**：per-thread 去重。同一线程的多次快速交互只处理最后一次。

---

## 步骤 ⑥：更新层（LLM 提取）

**文件**: `deerflow/agents/memory/updater.py`

```
_do_update_memory_sync(messages, thread_id, agent_name, user_id, ...)
  ├─ 1. 读取当前记忆（从 Storage + 缓存）
  ├─ 2. 格式化对话文本
  ├─ 3. 构建 LLM 提示词
  │     ├─ MEMORY_UPDATE_PROMPT（prompt.py）
  │     ├─ 包含当前记忆快照
  │     ├─ 包含对话内容
  │     └─ 包含纠正/强化提示
  ├─ 4. LLM 分析
  │     └─ create_chat_model(thinking_enabled=False)  ← 禁用思考模式
  │         标签: "memory_agent"
  ├─ 5. 解析 LLM 响应（JSON）
  │     ├─ 上下文更新（user/history 各字段）
  │     └─ 新事实列表（带置信度）
  ├─ 6. 应用更新
  │     ├─ _apply_updates()
  │     │     ├─ 上下文字段合并
  │     │     ├─ 事实去重（空格标准化后比较）
  │     │     ├─ 置信度过滤（≥ fact_confidence_threshold）
  │     │     └─ max_facts 限制（按置信度排序，保留前 N 条）
  │     └─ _strip_upload_mentions_from_memory()
  │           └─ 清除上传文件相关描述（防止跨会话混淆）
  └─ 7. 保存（见步骤 ⑦）
```

### 事实去重细节

```python
# 空格标准化：去除首尾空白后比较
def _fact_content_key(content):
    return content.strip().casefold() if content else None

# 已有事实的 key 集合
existing_keys = { _fact_content_key(f["content"]) for f in current_facts }

# 新事实只加入不重复的
for fact in new_facts:
    if _fact_content_key(fact["content"]) not in existing_keys:
        current_facts.append(fact)
```

---

## 步骤 ⑦：原子保存

**文件**: `deerflow/agents/memory/storage.py` → `save()`

```
save(memory_data, agent_name, user_id)
  ├─ 1. 更新 lastUpdated 时间戳
  ├─ 2. 计算目标路径（per-user 隔离）
  ├─ 3. 原子写入:
  │     ├─ temp_path = file_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
  │     ├─ json.dump(data, temp_path)
  │     └─ temp_path.replace(file_path)  ← POSIX 原子操作
  └─ 4. 更新缓存
        └─ _cache[key] = (data, new_mtime)
```

---

## 步骤 ⑧：Gateway API 集成

**文件**: `app/gateway/routers/memory.py`

```
GET  /api/memory              → 读取当前用户记忆
POST /api/memory/reload       → 强制重新加载（清除缓存）
GET  /api/memory/config       → 读取记忆配置
GET  /api/memory/status       → 配置 + 数据
POST /api/memory/facts        → 手动创建事实
DELETE /api/memory/facts/{id} → 删除特定事实
```

---

## 跨模块协作汇总

```
┌──────────────────────────────────────────────────────────────────┐
│                        记忆功能跨模块协作                          │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Storage ◂──读取──── DynamicContext ──注入──▸ HumanMessage      │
│    │                          │                        │         │
│    │                          │                        ▼         │
│    │                    （ID-swap 技术）            Agent 对话    │
│    │                                                  │         │
│    │                                                  ▼         │
│    │                                          MemoryMiddleware  │
│    │                                           │ after_agent()  │
│    │                                           ▼                │
│    │                                          Queue             │
│    │                                        (30s debounce)      │
│    │                                           │                │
│    │                                           ▼                │
│    │                                         Updater            │
│    │                                       (LLM 提取)           │
│    │                                           │                │
│    │                                           ▼                │
│    └──────────────────────────────────── 原子保存 (.json)        │
│                                                                  │
│  额外协作路径:                                                    │
│  ├── Summarization ↔ Memory: memory_flush_hook（摘要前提取）     │
│  ├── UserContext ↔ Memory: user_id 解析（per-user 隔离）         │
│  ├── Gateway ↔ Memory: API 端点查看/修改记忆                     │
│  └── Frontend ↔ Memory: 记忆状态展示                             │
└──────────────────────────────────────────────────────────────────┘
```

---

## 深入阅读

| 模块 | 文档 |
|------|------|
| 记忆系统设计 | [docs/core/memory/01-memory-system.md](../core/memory/01-memory-system.md) |
| 记忆设计决策 | [docs/core/memory/002-design-decisions.md](../core/memory/002-design-decisions.md) |
| 中间件详解 | [docs/core/agent/05-middlewares.md](../core/agent/05-middlewares.md) |
| 上下文压缩 | [02-context-compression.md](02-context-compression.md) |
| 配置系统 | [docs/core/config/04-feature-config.md](../core/config/04-feature-config.md) |
