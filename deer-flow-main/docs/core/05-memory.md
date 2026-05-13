# Memory 记忆系统——底层逻辑与本质

## 一句话本质

Memory = **写时分离**（中间件只排队，后台线程做 LLM 提取）+ **读时注入**（按 token 预算将记忆塞进系统提示词）。Agent 的"记忆"本质是对话历史的 LLM 提炼物，不是原始对话的存储。

---

## 1. 记忆的数据结构——三层模型

```
memory.json
├─ user（用户画像）
│   ├─ workContext:      "核心开发者，DeerFlow 项目，16k+ stars"（2-3 句）
│   ├─ personalContext:  "中英双语，偏好 Python，关注 AI Agent"（1-2 句）
│   └─ topOfMind:        "正在实现记忆系统、调研 RAG 方案、追踪..."（3-5 句）
│
├─ history（时间线上下文）
│   ├─ recentMonths:     "近 3 个月：完成 Agent 重构、..."（4-6 句/1-2 段）
│   ├─ earlierContext:   "3-12 个月前：研究了 LangGraph..."（3-5 句）
│   └─ longTermBackground: "长期背景：全栈工程师，10 年经验..."（2-4 句）
│
└─ facts[]（离散事实）
    ├─ { id: "fact_a1b2c3", content: "偏好 TypeScript 严格模式",
    │    category: "preference", confidence: 0.95, createdAt: "..." }
    ├─ { id: "fact_d4e5f6", content: "项目使用 pnpm monorepo",
    │    category: "knowledge", confidence: 0.85, createdAt: "..." }
    └─ ...（最多 100 条，按置信度淘汰）
```

**为什么分三层？** 因为不同信息的生命周期和使用场景不同：
- `user`：回答"这个用户是谁"，高优先级，每次注入
- `history`：回答"最近在忙什么"，中等优先级，提供时间线索
- `facts`：回答"用户有什么偏好/知识/目标"，按置信度竞争注入

**核心启示**：记忆不是"什么都记"，而是按**抽象粒度**分层存储。原始对话 → LLM 提炼 → 结构化存储。三层结构对应三个认知层级：身份（你是谁）、经历（你做了什么）、特征（你偏好什么）。

## 2. 写时分离——中间件只排队，不执行

```
用户消息 → Agent 执行 → MemoryMiddleware.after_agent()
                              │
                              ├─ 过滤消息（只保留 user + 最终 AI 响应）
                              ├─ 排入 MemoryUpdateQueue
                              └─ 返回 None（不阻塞 Agent）
                                       │
                                       ▼ (30 秒防抖后)
                              MemoryUpdater.update_memory()
                                       │
                                       ├─ 读取当前 memory.json
                                       ├─ 格式化对话 → 喂给 LLM
                                       ├─ LLM 返回 JSON 更新指令
                                       ├─ 应用更新（摘要替换 + 事实增删）
                                       ├─ 清除上传文件提及
                                       └─ 原子写入 memory.json
```

**为什么不在请求路径中更新记忆？** Agent 的响应延迟是用户体验的核心。LLM 提取事实需要一次额外的 LLM 调用（可能 5-10 秒），加上文件 I/O，如果同步执行，用户每次对话多等 10 秒。排入队列后立即返回，后台线程 30 秒后处理，用户无感知。

**核心启示**：这是 CQRS 模式在 Agent 领域的应用——读路径（注入记忆到 prompt）是同步的、廉价的（读文件 + 格式化）；写路径（LLM 提取事实）是异步的、昂贵的。中间件是两者的唯一桥梁，它只做"入队"这一个轻量操作。

## 3. 防抖 + 线程去重——高频对话的自然聚合

```python
def add(self, thread_id, messages, agent_name=None):
    # 去重：同一 thread_id 的旧条目被替换
    self._queue = [c for c in self._queue if c.thread_id != thread_id]
    self._queue.append(context)

    # 重置防抖计时器（30 秒）
    self._timer.cancel()
    self._timer = threading.Timer(30, self._process_queue)
    self._timer.start()
```

用户可能在 30 秒内连续发 5 条消息，每条触发一次 `after_agent`。防抖让系统在用户"停下来"后才开始处理，此时能看到完整对话脉络。线程去重确保同一次对话不会重复排队。

**核心启示**：记忆更新的粒度不应该是"每条消息"，而应该是"一次交互会话"。用户连续发多条消息时，只有最后一条 + 所有历史消息一起送给 LLM 分析，提取的事实质量更高（完整上下文 vs. 片段上下文）。

## 4. 事实的准入与淘汰——记忆不是垃圾桶

```
准入门槛：
  confidence >= 0.7（低于直接丢弃）
  content 非空 + strip 后非空
  content 去重（strip 后与已有事实比较）

淘汰规则：
  超过 100 条 → 按置信度排序，保留最高的

显式淘汰：
  LLM 可通过 factsToRemove 字段请求删除过时事实
  新事实与旧事实矛盾时，LLM 会标记旧事实删除
```

**为什么设 0.7 门槛？** LLM 是概率模型，低置信度的输出可能是幻觉。0.5 置信度（"用户似乎喜欢 X"）不值得存入长期记忆——它占了一个有限的事实槽位，但下次对话时可能被当成确定信息使用。

**核心启示**：有限容量 + 优胜劣汰。记忆系统不是无限存储——每个事实都消耗 prompt token（注入时）。设计一个"准入门槛 + 容量上限 + 淘汰策略"的三层机制，确保进入记忆的都是高确信度的信息。

## 5. Token 预算制的记忆注入——背包问题

```python
def format_memory_for_injection(memory_data, max_tokens=2000):
    # 1. 摘要和上下文：无条件包含
    sections.append("User Context:\n" + ...)
    sections.append("History:\n" + ...)

    # 2. 事实：按置信度降序，逐条填充剩余预算
    ranked_facts = sorted(facts, key=lambda f: f.confidence, reverse=True)
    for fact in ranked_facts:
        if running_tokens + fact_tokens <= max_tokens:
            fact_lines.append(fact)
        else:
            break  # 预算耗尽，停止

    return f"<memory>\n{result}\n</memory>"
```

记忆文件可能有 100 条事实 + 多个摘要，全部注入会占 5000+ token。`max_injection_tokens`（默认 2000）让记忆注入变成背包问题：摘要和上下文（结构化的、经过 LLM 精炼的）优先装入，事实按置信度降序逐条填充剩余空间。

**核心启示**：记忆的"存"和"用"是完全不同的操作。存储是宽松的（100 条事实、详细摘要），使用是精确计量的（2000 token 预算）。不要把所有存储的内容都注入 prompt——用背包算法在有限预算内装入最有价值的信息。

## 6. 原子写入——防崩溃保数据完整性

```python
def save(self, memory_data, agent_name=None):
    temp_path = file_path.with_suffix(".tmp")    # 写临时文件
    with open(temp_path, "w") as f:
        json.dump(memory_data, f, indent=2)

    temp_path.replace(file_path)                  # 原子替换（POSIX rename）
```

写入过程中如果进程崩溃（OOM、SIGKILL），直接写目标文件会留下半截损坏的 JSON，下次加载解析失败导致记忆丢失。先写临时文件再原子替换，确保目标文件要么是旧版本要么是新版本，不存在中间态。

**核心启示**：任何"读-改-写"循环的持久化操作都应该用原子写入。Agent 系统中的文件写入（记忆、配置、检查点）都可能被进程崩溃中断。临时文件 + rename 是成本最低的崩溃安全方案。
