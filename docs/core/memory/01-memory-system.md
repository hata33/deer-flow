# DeerFlow 记忆系统

> 记忆系统是 DeerFlow 的核心差异化功能。它让 Agent 跨会话记住用户偏好、知识背景、行为模式，并在后续对话中自动注入个性化上下文。

---

## 一、四层架构总览

```
┌─────────────────────────────────────────────────────────┐
│                    第 4 层：中间件                        │
│         MemoryMiddleware + Summarization Hook            │
│    after_agent() 钩子触发 → 防抖队列 (30s)                │
│    依赖：queue.py, message_processing.py                 │
└──────────────────────┬──────────────────────────────────┘
                       │ add(thread_id, messages)
┌──────────────────────▼──────────────────────────────────┐
│                    第 3 层：提取                          │
│                   MemoryUpdater                          │
│    LLM 分析对话 → JSON 更新指令 → _apply_updates()        │
│    依赖：prompt.py (MEMORY_UPDATE_PROMPT), storage.py     │
└──────────────────────┬──────────────────────────────────┘
                       │ save(memory_data)
┌──────────────────────▼──────────────────────────────────┐
│                    第 2 层：存储                          │
│         MemoryStorage (ABC) → FileMemoryStorage          │
│    memory.json: user context / history / facts           │
│    依赖：memory_config.py, paths.py                      │
└──────────────────────┬──────────────────────────────────┘
                       │ load(agent_name)
┌──────────────────────▼──────────────────────────────────┐
│                    第 1 层：注入                          │
│       prompt.py → format_memory_for_injection()          │
│    按置信度排序 facts → token 预算截断 → 注入 system prompt │
│    依赖：tiktoken (token 计数), memory_config.py          │
└─────────────────────────────────────────────────────────┘
```

---

## 二、第 2 层：存储（storage.py）

### 2.1 设计目标

需要持久化用户画像，支持"这个用户是做什么的、偏好什么技术、近期关注什么"。数据需要结构化以支持分类查询和置信度排序。

### 2.2 数据模型

**文件**：`agents/memory/storage.py`

```json
{
  "version": "1.0",
  "lastUpdated": "2026-05-20T08:00:00Z",
  "user": {
    "workContext":    {"summary": "...", "updatedAt": "..."},
    "personalContext": {"summary": "...", "updatedAt": "..."},
    "topOfMind":      {"summary": "...", "updatedAt": "..."}
  },
  "history": {
    "recentMonths":      {"summary": "...", "updatedAt": "..."},
    "earlierContext":    {"summary": "...", "updatedAt": "..."},
    "longTermBackground": {"summary": "...", "updatedAt": "..."}
  },
  "facts": [
    {
      "id": "fact_a1b2c3d4",
      "content": "用户偏好 TypeScript",
      "category": "preference",
      "confidence": 0.9,
      "createdAt": "2026-05-15T08:00:00Z",
      "source": "thread_abc123",
      "sourceError": "..."  // 仅 correction 类别
    }
  ]
}
```

| 分区 | 包含字段 | 更新频率 | 设计原因 |
|------|----------|----------|----------|
| `user` | workContext, personalContext, topOfMind | 中/高 | 刻画当前状态和最关注的事 |
| `history` | recentMonths, earlierContext, longTermBackground | 低/极低 | 按时间衰减保留历史背景 |
| `facts` | 离散事实列表 | 高 | 粒度的精确信息，支持增删改查 |

**Facts 六种分类**：

| 分类 | 含义 | 触发场景 |
|------|------|----------|
| `preference` | 偏好（工具、风格、语言） | "我更喜欢用 pnpm" |
| `knowledge` | 专业知识 | "我熟悉 Kubernetes" |
| `context` | 背景事实 | "我在字节跳动工作" |
| `behavior` | 行为模式 | "习惯先写测试再写代码" |
| `goal` | 目标 | "想学习 Rust" |
| `correction` | 纠错 | "不对，应该用方案 B" |

### 2.3 为什么用 JSON 文件而非数据库

- **简单部署**：无需额外数据库依赖，单文件即可工作
- **可换存储**：通过 `MemoryStorage` 抽象基类 + `storage_class` 配置，可替换为 PostgreSQL 等后端
- **原子写入**：写临时文件 + `os.replace()`（原子 rename），不会出现写一半的损坏文件
- **mtime 缓存**：基于文件修改时间的缓存，避免每次构建提示词时读磁盘
- **按用户 + 按智能体隔离**：`(user_id, agent_name)` 二级 key，不同智能体有独立记忆

### 2.4 依赖

- `memory_config.py`：`storage_path`、`storage_class` 配置
- `paths.py`：路径解析（`user_memory_file(user_id)`）
- `AGENT_NAME_PATTERN`：智能体名称校验，防止路径穿越

---

## 三、第 3 层：提取（updater.py）

### 3.1 设计目标

从自然语言对话中**自动**提取用户信息，不需要用户手动填写任何表单。LLM 分析对话，返回结构化的 JSON 更新指令。

### 3.2 更新流程

```
MemoryUpdater.update_memory()
  │
  ├─ 1. 加载当前 memory.json → current_memory
  ├─ 2. format_conversation_for_update(messages) → conversation_text
  │     ├─ 过滤：只保留 human + 无 tool_calls 的 AI
  │     ├─ 去除 <uploaded_files> 块（会话级数据不持久化）
  │     └─ 截断 > 1000 字的消息
  │
  ├─ 3. 构建 MEMORY_UPDATE_PROMPT → model.invoke(prompt)
  │     ├─ 注入 current_memory（让 LLM 看到已有记忆）
  │     ├─ 注入 conversation（本次对话）
  │     └─ 注入 correction_hint（检测到纠错/正面反馈时）
  │
  ├─ 4. 解析 LLM 返回的 JSON → update_data
  │     ├─ user sections：shouldUpdate=true 才更新
  │     ├─ history sections：同上
  │     ├─ newFacts：置信度 ≥ fact_confidence_threshold (0.7) 才入库
  │     ├─ factsToRemove：按 ID 删除
  │     └─ 去重：按 content.casefold() 去重
  │
  ├─ 5. _strip_upload_mentions_from_memory() → 清除文件上传相关句子
  ├─ 6. 按 confidence 排序，截断到 max_facts (100)
  └─ 7. storage.save() → 原子写入文件
```

### 3.3 纠错与正面反馈检测

**文件**：`agents/memory/message_processing.py`

在最近 6 条用户消息中匹配关键词模式：

| 信号 | 匹配模式（中英文） | 效果 |
|------|-------------------|------|
| **纠错** | "that's wrong", "you misunderstood", "不对", "你理解错了", "重试", "改用" | LLM 提示词中注入 `correction_hint`，要求生成 `correction` 类别 fact，confidence ≥ 0.95 |
| **正面反馈** | "yes exactly", "perfect", "that's right", "对，就是这样", "完全正确", "继续保持" | 注入 `reinforcement_hint`，要求生成 `preference`/`behavior` 类别 fact，confidence ≥ 0.9 |

### 3.4 同步执行策略

- `update_memory()` 检测是否有运行中的事件循环：有 → 提交到 `ThreadPoolExecutor`；无 → 直接同步调用
- `aupdate_memory()` 使用 `asyncio.to_thread()` 委托同步路径
- **关键原因**：使用同步 `model.invoke()` 而非异步 `model.ainvoke()`，避免跨事件循环共享 httpx AsyncClient 连接池导致崩溃（issue #2615）

### 3.5 依赖

- `prompt.py`：`MEMORY_UPDATE_PROMPT`（~120 行的详细提示词）
- `storage.py`：`get_memory_storage().save()`
- `message_processing.py`：消息过滤、纠错检测、正面反馈检测
- `memory_config.py`：`fact_confidence_threshold`、`max_facts`、`model_name`
- `models.create_chat_model()`：创建 LLM 实例

---

## 四、第 1 层：注入（prompt.py）

### 4.1 设计目标

在每次构建 Agent 时，将已存储的记忆格式化后注入 system prompt，让 LLM 了解"正在和谁对话"。

### 4.2 注入流程

```
format_memory_for_injection(memory_data, max_tokens=2000)
  │
  ├─ 1. User Context → "Work: ... / Personal: ... / Current Focus: ..."
  ├─ 2. History → "Recent: ... / Earlier: ... / Background: ..."
  ├─ 3. Facts → 按 confidence 降序
  │     ├─ 逐条加入，每加一条用 tiktoken 实时算 token
  │     ├─ 超出 max_tokens 停止
  │     └─ correction 类别特殊格式：追加 "(avoid: sourceError)"
  └─ 4. 返回格式化段落 → 注入 <memory> XML 块
```

### 4.3 关键决策

- **tiktoken 精确计数**：使用 `cl100k_base` 编码（GPT-4/3.5 使用的编码），fallback 到字符数 ÷ 4
- **置信度排序**：高置信度事实优先注入，低置信度事实在 token 不足时被裁剪
- **NaN/Inf 安全**：`_coerce_confidence()` 将非法置信度钳制到 [0,1]
- **注入时机**：Agent 构建时（`make_lead_agent()` → `_get_memory_context()`），不是运行时。这意味着**本轮更新的记忆，下一轮才能看到**

### 4.4 依赖

- `tiktoken`（可选，未安装时回退到字符估算）
- `memory_config.py`：`max_injection_tokens`
- `storage.py`：读取 `memory.json`

---

## 五、第 4 层：中间件（MemoryMiddleware + queue.py）

### 5.1 设计目标

在 Agent 对话完成后自动触发记忆更新，同时避免高频 LLM 调用。

### 5.2 两个触发入口

| 入口 | 时机 | 方法 |
|------|------|------|
| `MemoryMiddleware.after_agent()` | Agent 图执行完毕 | `queue.add()` — 标准防抖 |
| `memory_flush_hook()` | SummarizationMiddleware 即将丢弃消息 | `queue.add_nowait()` — 立即处理 |

### 5.3 防抖队列

**文件**：`agents/memory/queue.py`

```
MemoryMiddleware.after_agent()
  └─ queue.add(thread_id, messages, agent_name, user_id)
      ├─ 同 (thread_id, user_id, agent_name) 已有待处理 → 替换为最新
      ├─ 合并 correction_detected / reinforcement_detected 标志
      └─ 重置 threading.Timer（默认 30s）

Timer 到期
  └─ _process_queue()
      ├─ 取出所有待处理 → 逐个调用 MemoryUpdater.update_memory()
      └─ 多上下文间 sleep 0.5s 避免 LLM rate limit
```

**为什么用 `threading.Timer` 而非 `asyncio`**：
- 防抖队列是全局单例，可能被多个协程/线程并发访问
- `threading.Timer` 在独立线程上触发，不依赖调用方的事件循环
- 需要显式捕获 `user_id`：`threading.Timer` 触发时 `ContextVar` 不传播，所以在 `add()` 时就存入 `ConversationContext`

### 5.4 消息过滤

**文件**：`agents/memory/message_processing.py` → `filter_messages_for_memory()`

只保留：
- **Human 消息**：去除 `<uploaded_files>` 块。如果去除后为空，跳过该条及接下来的 AI 回复
- **无 tool_calls 的 AI 消息**：工具调用是执行细节，不是用户信息

### 5.5 配置

```yaml
# config.yaml → memory 段
memory:
  enabled: true
  injection_enabled: true
  storage_path: ""                       # 空 = 按用户隔离
  storage_class: "deerflow.agents.memory.storage.FileMemoryStorage"
  debounce_seconds: 30                   # 30s 无新消息才触发 LLM 更新
  max_facts: 100                         # facts 上限
  fact_confidence_threshold: 0.7         # 低于此值不存储
  max_injection_tokens: 2000             # 注入 system prompt 的 token 预算
  model_name: null                       # null = 使用默认模型
```

---

## 六、完整生命周期

```
第 N 轮对话
  ├─ make_lead_agent()
  │   └─ _get_memory_context() → 读取 memory.json → 注入 <memory>
  ├─ agent.astream() → 用户与 Agent 交互
  └─ MemoryMiddleware.after_agent()
      └─ queue.add() → 排队，等 30s

...用户可能继续对话，每次重置 timer...

30s 无新消息后
  ├─ _process_queue()
  │   └─ MemoryUpdater.update_memory()
  │       ├─ LLM 分析对话 → JSON 更新指令
  │       └─ storage.save() → 原子写入 memory.json

第 N+1 轮对话
  ├─ make_lead_agent()
  │   └─ _get_memory_context() → 读到上一轮更新的记忆 ✓
  └─ ...
```

**关键延迟**：本轮对话更新的记忆，下一轮构建 Agent 时才能注入。这是"构建时注入"设计的内在特性，而非 Bug。

---

## 七、文件索引

| 文件 | 层级 | 职责 |
|------|------|------|
| `agents/memory/storage.py` | 第 2 层 | `MemoryStorage` 抽象 + `FileMemoryStorage` 实现，mtime 缓存，原子写入 |
| `agents/memory/updater.py` | 第 3 层 | `MemoryUpdater`，LLM 驱动更新，纠错/反馈检测，facts 去重截断 |
| `agents/memory/prompt.py` | 第 1+3 层 | `MEMORY_UPDATE_PROMPT` + `format_memory_for_injection()`，tiktoken 计数 |
| `agents/memory/queue.py` | 第 4 层 | `MemoryUpdateQueue`，防抖队列，threading.Timer 实现 |
| `agents/memory/message_processing.py` | 第 3+4 层 | 消息过滤 `filter_messages_for_memory()`，纠错/反馈检测 |
| `agents/memory/summarization_hook.py` | 第 4 层 | SummarizationMiddleware 触发立即刷入记忆的钩子 |
| `agents/middlewares/memory_middleware.py` | 第 4 层 | `MemoryMiddleware.after_agent()` 排队触发 |
| `config/memory_config.py` | 配置 | `MemoryConfig`：enabled, debounce, thresholds, token limits |
