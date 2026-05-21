# 003 - 记忆系统实现机制深度分析

> 本文档基于 `backend/packages/harness/deerflow/agents/memory/` 目录下的源码，逐层拆解记忆系统的实现细节。回答的是"代码怎么写的、为什么这么写"。

---

## 一、模块依赖关系图

```
┌─────────────────────────────────────────────────────────────────┐
│                      调用方（外部世界）                           │
│                                                                  │
│  lead_agent/prompt.py          middlewares/                      │
│  ┌──────────────────────┐      ┌──────────────────────────┐     │
│  │ apply_prompt_template│      │ MemoryMiddleware          │     │
│  │  └─ _get_memory_     │      │ SummarizationMiddleware   │     │
│  │     context()        │      │  └─ _fire_hooks()         │     │
│  └──────────┬───────────┘      └──────┬──────────┬─────────┘     │
│             │                         │          │               │
│             │ ①注入                   │ ②防抖    │ ③立即刷入     │
└─────────────┼─────────────────────────┼──────────┼───────────────┘
              │                         │          │
┌─────────────▼─────────────────────────▼──────────▼───────────────┐
│                      memory 包（内部世界）                        │
│                                                                   │
│  __init__.py ─── 统一导出入口                                      │
│                                                                   │
│  ┌──────────────┐   ┌──────────────────┐   ┌───────────────┐    │
│  │ prompt.py    │   │ queue.py         │   │ storage.py    │    │
│  │              │   │                  │   │               │    │
│  │ ① 注入       │   │ ② 防抖队列       │   │ ④ 持久化      │    │
│  │ ③ 提取       │   │   threading.Timer│   │   mtime 缓存  │    │
│  │   提示词模板  │   │   ConversationCtx│   │   原子写入    │    │
│  └──────┬───────┘   └────────┬─────────┘   └───────▲───────┘    │
│         │                    │                      │            │
│         │              ┌─────▼──────────┐          │            │
│         │              │ updater.py     │          │            │
│         │              │                │          │            │
│         │              │ ③ LLM 提取     ├──────────┘            │
│         │              │   model.invoke │                       │
│         │              └─────┬──────────┘                       │
│         │                    │                                   │
│         │              ┌─────▼──────────────┐                   │
│         └──────────────┤ message_processing │                   │
│                        │                    │                   │
│                        │ 消息过滤            │                   │
│                        │ 信号检测            │                   │
│                        └────────────────────┘                   │
│                                                                   │
│  summarization_hook.py ─── 摘要前刷入钩子                          │
│                                                                   │
│  ┌──────────────────┐                                             │
│  │ memory_config.py │─── 全局配置（不在 memory/ 内）               │
│  └──────────────────┘                                             │
└───────────────────────────────────────────────────────────────────┘
```

---

## 二、第 1 层：注入 — 从磁盘到 Prompt

### 2.1 入口：`_get_memory_context()`

**文件**：`agents/lead_agent/prompt.py`

```python
def _get_memory_context(agent_name, *, app_config) -> str:
    # ① 检查开关
    if not config.enabled or not config.injection_enabled:
        return ""

    # ② 加载数据（通过 storage 层，带 mtime 缓存）
    memory_data = get_memory_data(agent_name, user_id=get_effective_user_id())

    # ③ 格式化 + token 截断
    memory_content = format_memory_for_injection(
        memory_data,
        max_tokens=config.max_injection_tokens   # 默认 2000
    )

    # ④ 包裹为 XML 标签
    return f"<memory>\n{memory_content}\n</memory>"
```

**调用时机**：`apply_prompt_template()` 构建 system prompt 时，将返回值填充到模板的 `{memory}` 占位符：

```
<role>You are DeerFlow 2.0...</role>
<soul>...</soul>
<memory>                         ← 注入点
User Context:
- Work: ...
Facts:
- [preference | 0.95] 用户偏好 TypeScript
</memory>
<thinking_style>...</thinking_style>
```

**关键细节**：

| 要点 | 实现 |
|------|------|
| 何时注入 | `make_lead_agent()` 构建时，不是运行时 |
| 隔离维度 | `(agent_name, user_id)` 二级 key |
| user_id 来源 | `get_effective_user_id()` → 无认证时为 `"default"` |
| 异常安全 | 整个函数 try/except 包裹，失败返回空字符串 |

### 2.2 格式化：`format_memory_for_injection()`

**文件**：`agents/memory/prompt.py`

这是注入层的核心函数，处理三段内容的格式化：

```
输入：memory_data 字典
      ↓
┌─────────────────────────────────────────┐
│ ① User Context                         │
│    Work: {workContext.summary}          │
│    Personal: {personalContext.summary}  │
│    Current Focus: {topOfMind.summary}   │
├─────────────────────────────────────────┤
│ ② History                              │
│    Recent: {recentMonths.summary}       │
│    Earlier: {earlierContext.summary}    │
│    Background: {longTerm.summary}       │
├─────────────────────────────────────────┤
│ ③ Facts（按 confidence 降序）            │
│    - [preference | 0.95] 偏好 TS        │
│    - [correction | 0.95] 应该用 B       │
│      (avoid: 方案 A)                    │
│    ...受 max_tokens 预算截断            │
└─────────────────────────────────────────┘
```

**Token 预算管理的实现**：

```python
# 1. 先算 User Context + History 的 token 数
base_tokens = _count_tokens(base_text)

# 2. 预留 "Facts:\n" 标题的 token
running_tokens = base_tokens + separator_tokens

# 3. 逐条加入 facts，每加一条实时计算增量 token
for fact in ranked_facts:
    line_tokens = _count_tokens(line_text)
    if running_tokens + line_tokens <= max_tokens:
        fact_lines.append(line)
        running_tokens += line_tokens
    else:
        break  # 超预算，停止
```

**correction 类别的特殊格式**：

```python
# 普通 fact
"- [preference | 0.95] 用户偏好 TypeScript"

# correction fact（带应避免的错误描述）
"- [correction | 0.95] 应该使用方案 B (avoid: 之前使用了方案 A)"
```

### 2.3 Token 计数策略

```python
def _count_tokens(text, encoding_name="cl100k_base"):
    if not TIKTOKEN_AVAILABLE:
        return len(text) // 4        # 回退：字符数 ÷ 4
    encoding = tiktoken.get_encoding(encoding_name)
    return len(encoding.encode(text))  # 精确：tiktoken 编码
```

选择 `cl100k_base` 编码（GPT-4/3.5 使用的编码），与大多数 LLM 的 tokenizer 对齐。

---

## 三、第 2 层：存储 — 文件系统上的 KV 数据库

### 3.1 路径解析逻辑

**文件**：`agents/memory/storage.py` → `_get_memory_file_path()`

```
输入：(user_id, agent_name) 组合
      ↓
┌──────────────────────────────────────────────────────────┐
│ user_id + agent_name ?                                   │
│   YES → {base}/users/{user_id}/agents/{agent_name}/     │
│         memory.json                                      │
│                                                          │
│ user_id only ?                                           │
│   YES → config.storage_path 为绝对路径 ?                 │
│           YES → 直接使用该路径（不按用户隔离）            │
│           NO  → {base}/users/{user_id}/memory.json       │
│                                                          │
│ agent_name only (无 user_id) ?                           │
│   YES → {base}/agents/{agent_name}/memory.json           │
│                                                          │
│ 都没有 ?                                                  │
│   → config.storage_path ?                                │
│     YES → 绝对路径 / {base}/{相对路径}                    │
│     NO  → {base}/memory.json                             │
└──────────────────────────────────────────────────────────┘
```

**路径安全校验**：`_validate_agent_name()` 使用 `AGENT_NAME_PATTERN` 正则确保智能体名称不含 `..` 或 `/` 等路径穿越字符。

### 3.2 mtime 缓存机制

```python
def load(self, agent_name, *, user_id):
    # ① 获取文件当前 mtime
    current_mtime = file_path.stat().st_mtime

    # ② 与缓存比较
    cached = self._memory_cache.get(cache_key)
    if cached is not None and cached[1] == current_mtime:
        return cached[0]         # mtime 未变 → 返回缓存

    # ③ mtime 变了 → 重新读文件 + 更新缓存
    memory_data = self._load_memory_from_file(...)
    self._memory_cache[cache_key] = (memory_data, current_mtime)
    return memory_data
```

**为什么不用 TTL 缓存**：`make_lead_agent()` 每次构建 Agent 都会调用 `load()`。外部手动编辑 `memory.json` 时 mtime 会变，缓存自动失效——比固定 TTL 更灵活且无额外开销。

### 3.3 原子写入流程

```python
def save(self, memory_data, agent_name, *, user_id):
    # ① 浅拷贝 + 设置时间戳（避免修改调用方的原始 dict）
    memory_data = {**memory_data, "lastUpdated": utc_now_iso_z()}

    # ② 写入随机命名的临时文件
    temp_path = file_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    with open(temp_path, "w") as f:
        json.dump(memory_data, f, indent=2, ensure_ascii=False)

    # ③ 原子重命名（POSIX: atomic replace; Windows: reliable replace）
    temp_path.replace(file_path)

    # ④ 更新缓存
    self._memory_cache[cache_key] = (memory_data, mtime)
```

**为什么要浅拷贝**：注释写得很清楚——"so the caller's dict is not mutated as a side-effect, and the cache reference is not silently updated before the file write succeeds"。如果 `save()` 在写入磁盘前失败，不应污染调用方持有的数据。

### 3.4 存储后端替换：工厂模式

```python
def get_memory_storage() -> MemoryStorage:
    # 反射加载：从 "module.path.ClassName" 格式解析
    module_path, class_name = storage_class_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    storage_class = getattr(module, class_name)

    # 类型校验
    if not issubclass(storage_class, MemoryStorage):
        raise TypeError(...)

    _storage_instance = storage_class()
```

配置示例（切换到 PostgreSQL 后端）：

```yaml
memory:
  storage_class: "myapp.storage.PostgresMemoryStorage"
```

---

## 四、第 3 层：提取 — LLM 理解对话

### 4.1 完整的更新流水线

```
MemoryUpdater._do_update_memory_sync()
  │
  ├─ _prepare_update_prompt()
  │   ├─ get_memory_data()              ← 加载当前记忆
  │   ├─ format_conversation_for_update() ← 格式化对话
  │   └─ _build_correction_hint()       ← 构建纠错/反馈提示
  │
  ├─ model.invoke(prompt)               ← LLM 分析（同步 HTTP）
  │
  └─ _finalize_update()
      ├─ _extract_text()                ← 提取 LLM 响应文本
      ├─ 去除 markdown 代码块包裹
      ├─ json.loads()                   ← 解析 JSON 更新指令
      ├─ _apply_updates()               ← 合并到记忆数据
      ├─ _strip_upload_mentions()       ← 清除上传相关内容
      └─ storage.save()                 ← 原子写入
```

### 4.2 对话预处理：`format_conversation_for_update()`

```
原始消息列表                          处理后
───────────────────                   ──────────────
Human: <uploaded_files>file1.pdf      （跳过：纯上传消息）
AI: [tool_call: read_file(...)]       （跳过：工具调用）
AI: 这是文件内容...                   User: 这是我的需求
Human: 这是我的需求                   Assistant: 好的，我来帮你
AI: 好的，我来帮你                    User: 不对，应该用方案B
Human: 不对，应该用方案B（5000字）     Assistant: 明白了，改用方案B
AI: 明白了，改用方案B                 ↓
                                      （"不对" 触发纠错信号检测）
```

关键过滤规则：
- `<uploaded_files>` 标签正则移除，移除后为空则跳过整条
- 有 `tool_calls` 的 AI 消息跳过（执行细节不是用户信息）
- > 1000 字的消息截断（防止消耗过多 token）

### 4.3 LLM 返回的 JSON 结构

LLM 被要求返回严格格式的 JSON：

```json
{
  "user": {
    "workContext": { "summary": "...", "shouldUpdate": true },
    "personalContext": { "summary": "...", "shouldUpdate": false },
    "topOfMind": { "summary": "...", "shouldUpdate": true }
  },
  "history": {
    "recentMonths": { "summary": "...", "shouldUpdate": true },
    "earlierContext": { "summary": "...", "shouldUpdate": false },
    "longTermBackground": { "summary": "...", "shouldUpdate": false }
  },
  "newFacts": [
    { "content": "用户偏好方案 B", "category": "preference", "confidence": 0.95 },
    { "content": "之前方案 A 是错误的", "category": "correction", "confidence": 0.95 }
  ],
  "factsToRemove": ["fact_a1b2c3d4"]
}
```

**`shouldUpdate` 的作用**：只有 `shouldUpdate=true` 且 `summary` 非空时才覆盖对应 section，避免 LLM 返回空摘要覆盖有效数据。

### 4.4 `_apply_updates()` 的合并逻辑

```python
def _apply_updates(self, current_memory, update_data, thread_id):
    # ① Sections：shouldUpdate=true 才覆盖
    for section in ["workContext", "personalContext", "topOfMind"]:
        if section_data.get("shouldUpdate") and section_data.get("summary"):
            current_memory["user"][section] = {
                "summary": section_data["summary"],
                "updatedAt": now,
            }

    # ② 删除 facts：按 ID 列表
    facts_to_remove = set(update_data.get("factsToRemove", []))
    current_memory["facts"] = [
        f for f in facts if f.get("id") not in facts_to_remove
    ]

    # ③ 新增 facts：去重 + 阈值过滤
    for fact in new_facts:
        if confidence >= config.fact_confidence_threshold:  # 默认 0.7
            fact_key = _fact_content_key(normalized_content)  # casefold
            if fact_key in existing_fact_keys:
                continue  # 内容重复，跳过
            current_memory["facts"].append(fact_entry)

    # ④ 强制截断：超过 max_facts 按置信度排序保留
    if len(current_memory["facts"]) > config.max_facts:  # 默认 100
        current_memory["facts"] = sorted(
            current_memory["facts"],
            key=lambda f: f.get("confidence", 0),
            reverse=True,
        )[:config.max_facts]
```

**去重键的生成**：`_fact_content_key()` 返回 `content.strip().casefold()`，即去除首尾空白后大小写不敏感比较。

### 4.5 上传文件内容清除

两处清除机制：

**第一处**：`format_conversation_for_update()` — 从输入侧过滤

```python
if role == "human":
    content = re.sub(r"<uploaded_files>[\s\S]*?</uploaded_files>\n*", "", str(content))
```

**第二处**：`_strip_upload_mentions_from_memory()` — 从输出侧清除

```python
# 匹配上传事件句子的窄正则
_UPLOAD_SENTENCE_RE = re.compile(
    r"[^.!?]*\b(?:"
    r"upload(?:ed|ing)?(?:\s+\w+){0,3}\s+(?:file|files?|document|...)"
    r"|/mnt/user-data/uploads/"
    r"|<uploaded_files>"
    r")[^.!?]*[.!?]?\s*",
    re.IGNORECASE,
)
```

刻意使用窄匹配，避免误删 "User works with CSV files" 等合法 fact。

### 4.6 同步 vs 异步：issue #2615 的教训

```
问题链：
  langchain 通过 @lru_cache 全局缓存一个 httpx.AsyncClient
       ↓
  主 Agent 和记忆更新共享这个 AsyncClient
       ↓
  如果记忆更新用 asyncio.run() 创建新事件循环
       ↓
  新循环中复用旧循环的连接 → crash

解决方案：
  记忆更新使用 model.invoke()（同步 HTTP）
  走独立的同步 httpx 连接池，完全隔离
```

**三种调用路径的调度策略**：

```python
def update_memory(self, ...):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # 路径 A：在事件循环内 → ThreadPoolExecutor 卸载
        future = _SYNC_MEMORY_UPDATER_EXECUTOR.submit(
            self._do_update_memory_sync, ...
        )
        return future.result()  # 阻塞等待结果
    else:
        # 路径 B：不在事件循环 → 直接同步执行
        return self._do_update_memory_sync(...)

async def aupdate_memory(self, ...):
    # 路径 C：显式 async → asyncio.to_thread 卸载
    return await asyncio.to_thread(
        self._do_update_memory_sync, ...
    )
```

三条路径最终都汇聚到 `_do_update_memory_sync()`，保证一致性。

---

## 五、第 4 层：中间件触发 — 防抖队列

### 5.1 两个触发入口的调用链

**入口 A：标准防抖（Agent 对话结束）**

```
用户发送消息
  → agent.astream() → ReAct 循环执行
  → MemoryMiddleware.after_agent(state, runtime)
    → filter_messages_for_memory(messages)
    → detect_correction(filtered)
    → detect_reinforcement(filtered)
    → user_id = get_effective_user_id()        ← 在请求上下文内捕获
    → queue.add(thread_id, messages, user_id)  ← 30s 防抖
```

**入口 B：立即刷入（摘要前抢救）**

```
对话接近 token 限制
  → SummarizationMiddleware._maybe_summarize()
    → _should_summarize() = True
    → cutoff_index = ...
    → messages_to_summarize, preserved = ...
    → _fire_hooks(messages_to_summarize, ...)
      → memory_flush_hook(event)
        → filter_messages_for_memory(...)
        → detect_correction / detect_reinforcement
        → user_id = resolve_runtime_user_id(event.runtime)
        → queue.add_nowait(thread_id, messages, ...)  ← 立即处理
    → summary = self._create_summary(...)     ← 消息被摘要替换
```

**为什么入口 B 必须用 `add_nowait`**：`_fire_hooks()` 之后紧接着就是 `_create_summary()`，消息即将被摘要替代。如果用 30s 防抖，等处理时原始消息已经丢失了。

### 5.2 防抖队列内部状态机

```
                    add()
                      │
                      ▼
              ┌───────────────┐
              │  入队 + 合并   │
              │  (同键替换)    │
              └───────┬───────┘
                      │
              ┌───────▼───────┐
              │  重置 Timer   │──── 30s 内又有 add() ──→ 回到"重置 Timer"
              └───────┬───────┘
                      │ 30s 到期
                      ▼
              ┌───────────────┐
              │ _process_queue│
              │               │
              │ ① 取出所有项  │
              │ ② 清空队列    │
              │ ③ 逐个更新    │
              │ ④ sleep 0.5s  │──── 多个上下文间限速
              └───────────────┘
```

**同键合并的实现**：

```python
def _enqueue_locked(self, *, thread_id, messages, ...):
    # 查找同键已有条目
    existing = next(
        (ctx for ctx in self._queue
         if self._queue_key(ctx.thread_id, ctx.user_id, ctx.agent_name) == key),
        None,
    )
    # 合并信号标志（任一次为 True 则保持 True）
    merged_correction = correction_detected or (
        existing.correction_detected if existing else False
    )
    # 移除旧条目，追加新条目（消息替换为最新）
    self._queue = [ctx for ctx in self._queue if key != current_key]
    self._queue.append(new_context)
```

### 5.3 threading.Timer 的选择理由

```
问题：MemoryUpdateQueue 是全局单例
      ↓
可能被多个协程/线程并发访问：
  - LangGraph 主循环（asyncio）
  - threading.Timer 回调（独立线程）
  - 多个并发请求的 MemoryMiddleware
      ↓
asyncio 方案的问题：
  - 需要绑定到特定事件循环
  - 但调用方可能在不同循环中
  - 全局单例无法选择"正确的"循环
      ↓
threading.Timer 的优势：
  - 在独立线程触发，不依赖任何事件循环
  - 与 _lock 配合即可保证线程安全
      ↓
代价：
  - ContextVar 不跨线程传播
  - 必须在 add() 时显式捕获 user_id
```

### 5.4 user_id 的传递链

```
HTTP 请求上下文
  → ContextVar["user_id"] = "alice"
  → MemoryMiddleware.after_agent()
    → user_id = get_effective_user_id()      ← 从 ContextVar 读取
    → queue.add(user_id="alice")             ← 显式传入
    → ConversationContext(user_id="alice")    ← 存入数据结构
      ↓
    ... 30s 后 Timer 在独立线程触发 ...
      ↓
    → _process_queue()
      → MemoryUpdater.update_memory(user_id="alice")  ← 从 ConversationContext 取
      → storage.save(user_id="alice")                 ← 传给存储层
      → 写入 users/alice/memory.json                  ← 正确隔离
```

如果不在 `add()` 时捕获 `user_id`，Timer 线程中 `ContextVar` 为空，用户数据会写入错误位置。

---

## 六、信号检测机制

### 6.1 纠错信号检测

**文件**：`agents/memory/message_processing.py`

```python
# 11 个中英文正则模式
_CORRECTION_PATTERNS = (
    re.compile(r"\bthat(?:'s| is) (?:wrong|incorrect)\b", ...),
    re.compile(r"\byou misunderstood\b", ...),
    re.compile(r"\btry again\b", ...),
    re.compile(r"\bredo\b", ...),
    re.compile(r"不对"),
    re.compile(r"你理解错了"),
    re.compile(r"你理解有误"),
    re.compile(r"重试"),
    re.compile(r"重新来"),
    re.compile(r"换一种"),
    re.compile(r"改用"),
)

def detect_correction(messages):
    # 只看最近 6 条用户消息
    recent = [msg for msg in messages[-6:] if msg.type == "human"]
    for msg in recent:
        if any(pattern.search(content) for pattern in _CORRECTION_PATTERNS):
            return True
    return False
```

**检测到纠错后的效果链**：

```
detect_correction() = True
  → correction_detected=True 传入 queue.add()
  → ConversationContext 中保存
  → MemoryUpdater._build_correction_hint()
    → 注入到 MEMORY_UPDATE_PROMPT 的 {correction_hint}
    → "record the correct approach as a fact with category 'correction'
       and confidence >= 0.95"
  → LLM 倾向于生成高置信度 correction fact
```

### 6.2 优先级规则

```python
# 在 MemoryMiddleware.after_agent() 中：
correction_detected = detect_correction(filtered_messages)
reinforcement_detected = not correction_detected and detect_reinforcement(filtered_messages)
```

纠错优先于正面反馈。如果同时存在纠错和正面反馈，两个提示都会注入 `_build_correction_hint()`：

```python
def _build_correction_hint(self, correction_detected, reinforcement_detected):
    hint = ""
    if correction_detected:
        hint = "...correction >= 0.95..."
    if reinforcement_detected:
        reinforcement = "...preference/behavior >= 0.9..."
        hint = (hint + "\n" + reinforcement).strip() if hint else reinforcement
    return hint
```

---

## 七、配置参数全表

**文件**：`config/memory_config.py`

| 参数 | 类型 | 默认值 | 范围 | 作用 |
|------|------|--------|------|------|
| `enabled` | bool | `True` | — | 总开关：禁用后不入队、不注入 |
| `injection_enabled` | bool | `True` | — | 注入开关：可单独禁用注入（仍更新） |
| `storage_path` | str | `""` | — | 空=按用户隔离；绝对路径=共享 |
| `storage_class` | str | `FileMemoryStorage` | — | 存储后端类路径 |
| `debounce_seconds` | int | `30` | 1-300 | 防抖等待时间（秒） |
| `model_name` | str/None | `None` | — | 记忆更新使用的 LLM，None=默认模型 |
| `max_facts` | int | `100` | 10-500 | facts 数量上限 |
| `fact_confidence_threshold` | float | `0.7` | 0-1 | fact 入库的最低置信度 |
| `max_injection_tokens` | int | `2000` | 100-8000 | 注入 system prompt 的 token 预算 |

**配置加载链**：

```python
# config.yaml → AppConfig.memory → MemoryConfig
config_dict = yaml_config.get("memory", {})
_memory_config = MemoryConfig(**config_dict)
```

---

## 八、完整生命周期追踪（从一行代码到一行数据）

以用户发送 "我更喜欢用 TypeScript，不对，应该用 TypeScript 5.0" 为例：

```
T+0.0s  用户发送消息
T+0.1s  agent.astream() 开始
        → LLM 回复 "好的，我了解了您的偏好"
T+0.5s  MemoryMiddleware.after_agent() 被调用
        → filter_messages_for_memory()
          保留: Human("我更喜欢用 TypeScript...不对...TypeScript 5.0")
                AI("好的，我了解了")
        → detect_correction() = True  ← 匹配到 "不对"
        → detect_reinforcement() = False  ← 被短路
        → user_id = get_effective_user_id() = "alice"
        → queue.add(thread_id="t1", user_id="alice",
                    correction_detected=True)
        → Timer 开始计时：30s

T+5.0s  用户又发消息 "还有，我熟悉 Kubernetes"
        → 新一轮 agent.astream()
T+5.5s  MemoryMiddleware.after_agent() 再次被调用
        → queue.add(thread_id="t1", user_id="alice")
        → 同键替换：消息更新为最新完整对话
        → Timer 重置：重新计时 30s

T+35.5s 30s 无新消息，Timer 到期
        → _process_queue() 在独立线程执行
        → MemoryUpdater.update_memory()
          → 加载 users/alice/memory.json
          → format_conversation_for_update() → 对话文本
          → _build_correction_hint(correction_detected=True)
            → 注入 "correction >= 0.95" 提示
          → model.invoke(prompt) → LLM 返回 JSON
            → newFacts: [
                {content: "用户偏好 TypeScript 5.0",
                 category: "preference", confidence: 0.95},
                {content: "用户熟悉 Kubernetes",
                 category: "knowledge", confidence: 0.9}
              ]
          → _apply_updates()
            → 新 facts 通过去重 + 阈值检查
            → 追加到 memory_data["facts"]
          → _strip_upload_mentions() → 无变化
          → storage.save() → 原子写入

T+36.0s 下次 make_lead_agent()
        → storage.load() → mtime 变化 → 重新读取
        → format_memory_for_injection()
          → Facts 排序后注入：
            [preference | 0.95] 用户偏好 TypeScript 5.0
            [knowledge | 0.90] 用户熟悉 Kubernetes
```

---

## 九、文件职责速查表

| 文件 | 代码行 | 核心职责 | 关键类/函数 |
|------|--------|----------|------------|
| `storage.py` | ~230 | 持久化读写 | `FileMemoryStorage`、`get_memory_storage()` |
| `prompt.py` | ~360 | 注入格式化 + 提示词模板 | `format_memory_for_injection()`、`MEMORY_UPDATE_PROMPT` |
| `updater.py` | ~610 | LLM 提取 + Fact CRUD | `MemoryUpdater`、`_apply_updates()` |
| `queue.py` | ~290 | 防抖队列 | `MemoryUpdateQueue`、`ConversationContext` |
| `message_processing.py` | ~110 | 消息过滤 + 信号检测 | `filter_messages_for_memory()`、`detect_correction()` |
| `summarization_hook.py` | ~35 | 摘要前刷入 | `memory_flush_hook()` |
| `__init__.py` | ~60 | 统一导出 | `__all__` 列表 |

**外部依赖**：

| 文件 | 位置 | 职责 |
|------|------|------|
| `memory_config.py` | `config/` | `MemoryConfig` Pydantic 模型 |
| `memory_middleware.py` | `agents/middlewares/` | `MemoryMiddleware.after_agent()` |
| `summarization_middleware.py` | `agents/middlewares/` | `_fire_hooks()` 调用钩子 |
| `lead_agent/prompt.py` | `agents/lead_agent/` | `_get_memory_context()` 注入 |
| `lead_agent/agent.py` | `agents/lead_agent/` | `apply_prompt_template()` 组装 |
