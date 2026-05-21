# 002 - 设计决策与考量

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

---

## 一、核心设计决策清单

| # | 决策 | 一句话动机 |
|---|------|-----------|
| 1 | **构建时注入而非运行时注入** | 简单可靠，避免中间件在每次 model call 做记忆查询 |
| 2 | **JSON 文件存储 + 抽象基类** | 零依赖部署，可替换后端 |
| 3 | **Facts + Sections 双轨模型** | Facts 精确可排序，Sections 携带叙事上下文 |
| 4 | **LLM 驱动更新而非规则提取** | 对话中的隐含信息规则无法穷举 |
| 5 | **防抖队列 30s** | 避免高频 LLM 调用，节省成本，合并同一线程的连续更新 |
| 6 | **sync model.invoke() 而非 async** | 避免跨事件循环共享 httpx 连接池导致 crash |
| 7 | **threading.Timer 而非 asyncio** | 全局单例被多协程共享，不依赖特定事件循环 |
| 8 | **置信度排序 + token 预算截断** | 在有限空间内优先注入最可靠的信息 |
| 9 | **纠错/正面反馈信号检测** | LLM 说错话被纠正时，高置信度覆盖错误 fact |
| 10 | **上传事件自动清除** | 会话级文件路径在下轮不存在，持久化导致混淆 |

---

## 二、逐决策分析

### 决策 1：构建时注入 vs 运行时注入

**问题**：记忆数据何时进入 LLM context？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 构建时注入（当前） | 一次读取，整个会话可见 | 本轮更新的记忆下轮生效 |
| 运行时注入 | 实时性最好 | 每次 model call 读文件；中间件侵入所有调用 |

**选择构建时注入**：System prompt 在 Agent 生命周期只构建一次，ReAct 循环的所有步骤共享。记忆更新频率远低于对话频率（几分钟一次 vs 几秒一轮），延迟一轮用户无感知。

---

### 决策 2：JSON 文件 + 抽象基类

**为什么不强制用 PostgreSQL**：DeerFlow 最小部署是单容器 + 本地文件系统，强制数据库提高门槛。JSON 可被用户直接编辑、备份、迁移。

**为什么加 `MemoryStorage` 抽象基类**：通过 `storage_class` 配置可替换后端。`get_memory_storage()` 反射加载，失败 fallback 到 `FileMemoryStorage`。

**mtime 缓存**：`make_lead_agent()` 每次调用都读记忆。用文件 mtime 判断缓存有效性——外部手动编辑 JSON 能被检测到，优于固定 TTL。

**原子写入**：`uuid.tmp → os.replace()` 是 POSIX 原子操作，不会出现写了一半的损坏文件。

---

### 决策 3：Facts + Sections 双轨模型

**为什么不能全是 facts**："用户近期关注微服务转型" 是叙事总结，拆成 facts 丢失上下文。

**为什么不能全是 sections**："用户偏好 pnpm 而非 npm" 需按置信度排序和单独删除，sections 不支持。

| | Facts | Sections |
|---|-------|----------|
| 粒度 | 单条 statement | 叙事段落 |
| 排序 | 按 confidence 降序 | 固定顺序 |
| 操作 | 增删、去重、截断 | shouldUpdate 控制覆盖 |

**六种分类意图**：`preference`/`behavior` 个性化风格，`knowledge`/`context` 理解背景，`goal` 推进目标，`correction` 避免重复犯错（带 `sourceError` 字段）。

---

### 决策 4：LLM 驱动更新

**为什么不用规则提取**："我在字节做 infra" 隐含工作信息，"这个方案太繁琐了" 隐含偏好——自然语言的隐含信息正则无法穷举。

LLM 理解上下文、推断置信度、判断信息时效性。代价是每次更新消耗 LLM token，防抖队列控频。

---

### 决策 5：防抖队列 30s

**为什么不用即时更新**：用户快速连续对话（如 "帮我改这个 bug → 不对，应该用方案B → 再加个测试"），每句话触发一次 LLM 更新浪费且产生矛盾中间态。

**30s 的选择**：足够短让下轮对话读到最新记忆，足够长覆盖多轮连续对话。可配（`debounce_seconds: 1-300`）。

**同线程去重**：同 `(thread_id, user_id, agent_name)` 的新消息替换旧消息，只处理最新完整对话。

---

### 决策 6：sync model.invoke() 而非 async

**问题**：记忆更新是后台线程触发的，如果用 `model.ainvoke()` 会创建新事件循环，与主 Agent 共享 langchain 全局缓存的 httpx `AsyncClient` 连接池，跨循环复用连接导致 crash（issue #2615）。

**解决**：`_do_update_memory_sync()` 使用同步 `model.invoke()`，走独立的同步 httpx 连接池。调用方 `update_memory()` 检测所在事件循环，提交到 `ThreadPoolExecutor`。

---

### 决策 7：threading.Timer 而非 asyncio

**问题**：`MemoryUpdateQueue` 是全局单例，被不同线程和事件循环共享。

`threading.Timer` 在独立线程触发，不依赖任何特定事件循环。代价是需要显式捕获 `user_id`（`ContextVar` 不跨线程传播），在 `add()` 时存入 `ConversationContext`。

---

### 决策 8：置信度排序 + token 截断

**问题**：`max_injection_tokens=2000`，facts 可能远超此限制。

按 confidence 降序注入，低置信度在 token 不足时被裁剪。`_coerce_confidence()` 对 NaN/Inf 钳制到 [0,1]。`correction` 类别特殊格式 "`(avoid: sourceError)`"，占用更多 token 但信息密度高。

---

### 决策 9：纠错/正面反馈检测

**问题**：LLM 偶尔犯错被用户纠正，记忆系统需要感知。

在最近 6 条用户消息中匹配中英文关键词（"不对"/"that's wrong"/"你理解错了"/"重试" 等 11 个模式）。检测到纠错时在 `MEMORY_UPDATE_PROMPT` 中注入提示，要求生成 `correction` 类别 fact（confidence ≥ 0.95），覆盖之前的错误信息。

正面反馈同理（"完全正确"/"perfect"/"正是我想要的"），但纠错优先级更高（`reinforcement_detected` 仅在无纠错时检测）。

---

### 决策 10：上传事件清除

**问题**：用户上传文件路径如 `/mnt/user-data/uploads/report.pdf` 写入 facts，下轮文件已不存在。

**两处清除**：
- `format_conversation_for_update()`：human 消息中 `<uploaded_files>` 块被正则移除，移除后为空则整条跳过
- `_strip_upload_mentions_from_memory()`：所有 sections 摘要和 facts 中的上传相关句子被正则过滤，空 facts 被删除

---

## 三、实现效果

| 效果 | 实现方式 |
|------|----------|
| **跨会话个性化** | 第 N+1 轮 Agent 自动知道用户的技术栈、偏好、当前关注点 |
| **零配置部署** | JSON 文件自动创建，无需数据库 |
| **自动纠错记忆** | 用户说"不对"时高置信度覆盖错误 fact |
| **成本可控** | 防抖 30s + 批次合并，连续对话只触发一次 LLM 更新 |
| **多智能体隔离** | 不同 Agent 有独立 `memory.json` |
| **多用户隔离** | `(user_id, agent_name)` 二级 key |
| **可扩展存储** | 抽象基类 + 工厂模式，换 PostgreSQL 只需改配置 |
