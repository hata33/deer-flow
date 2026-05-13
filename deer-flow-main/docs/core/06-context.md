# 上下文管理——底层逻辑与本质

## 一句话本质

上下文管理 = **控制进入 LLM 的信息量**。对话历史无限增长，LLM 的 context window 有限。管理策略是：压缩旧消息 + 检测重复调用 + 用量可观测，三管齐下防止 token 爆炸。

---

## 1. 问题根源——对话是单向膨胀的

```
Turn 1: user msg (100 token) + AI response (200 token) = 300 token
Turn 2: 历史 300 + user msg (100) + tool call (50) + tool result (500) + AI response (200) = 1150 token
Turn 3: 历史 1150 + ...
Turn 10: 可能 15000+ token
Turn 20: 可能 50000+ token → 超出 context window → LLM 报错或截断
```

对话消息只增不减（LangGraph 的 `messages` 列表是 append-only），但 LLM 的输入有上限。上下文管理的本质是：**在消息量接近上限前，主动压缩历史，为新对话腾出空间。**

## 2. SummarizationMiddleware——触发式历史压缩

### 触发条件（OR 逻辑，任一满足即触发）

```yaml
# config.yaml
summarization:
  enabled: true
  trigger:
    - type: tokens
      value: 4000        # 历史超过 4000 token 时触发
    - type: messages
      value: 50          # 消息数超过 50 时触发
    - type: fraction
      value: 0.8         # 达到模型最大输入的 80% 时触发
```

### 压缩流程

```
全部消息历史（50 条消息，8000 token）
  │
  ├─ 保留最近 N 条（keep 策略）
  │   [消息 31-50] → 不动
  │
  └─ 压缩较早的消息
      [消息 1-30] → 发给 LLM 生成摘要（控制在 trim_tokens 内）
                    → 摘要作为 HumanMessage 替换原始消息
  │
  ▼
压缩后（摘要 500 token + 消息 31-50 = 3000 token）
```

**关键设计决策**：
- **AI + Tool 消息成对保留**：截断点不会拆散 `tool_calls` + `ToolMessage` 序列
- **用独立模型做摘要**：主模型可能很贵（GPT-4），摘要用便宜模型（GPT-4o-mini）
- **摘要也是 messages 的一部分**：被 LangGraph 检查点持久化，重启后不丢失

**核心启示**：上下文压缩不是"删除旧消息"，而是"用更少的 token 表达相同信息"。摘要保留了关键信息（做了什么、决定了什么、结果是什么），丢弃了中间过程（工具调用的详细参数、冗长的中间输出）。这和人类记忆的工作方式一致——你记得结论，不记得推理过程。

## 3. 保留策略——不是所有消息都值得保留

```yaml
keep:
  type: messages
  value: 20    # 始终保留最近 20 条消息
```

保留策略确保最近的交互始终以原始粒度存在——用户刚说的话、Agent 刚做的操作，一字不差。只有"较老"的消息才被压缩。这很重要，因为：
- 最近的对话是当前任务的直接上下文，压缩会丢失关键细节
- 较老的对话可能已经不相关了，压缩对当前任务影响最小

**核心启示**：上下文管理的关键决策不是"压缩什么"，而是"保留什么"。保留区域是 Agent 的"工作记忆"，压缩区域是"长期记忆"的近似。保留区的大小取决于任务复杂度——简单问答保留 5 条够了，复杂编程任务可能需要 30 条。

## 4. LoopDetectionMiddleware——防止上下文被循环调用撑爆

即使没有 SummarizationMiddleware（它默认关闭），系统仍有兜底机制防止上下文爆炸：

```
after_model（LLM 输出后、工具执行前）
  │
  ├─ 对本次 tool_calls 做 MD5 哈希（排序后，与顺序无关）
  ├─ 与滑动窗口中最近 20 次调用的哈希比较
  │
  ├─ 重复 3 次 → 注入 HumanMessage："你正在重复相同的工具调用，停止并给出最终答案"
  │               （只警告一次，使用 HumanMessage 而非 SystemMessage 避免 Anthropic 限制）
  │
  └─ 重复 5 次 → 强制清空 tool_calls，LLM 只能输出文本响应
                  （硬截断，Agent 被迫停下来）
```

**为什么 LLM 会循环？** LLM 没有内置的"我刚才做过这个了"检测。如果工具返回的结果不是 LLM 预期的，它可能反复调用同一个工具，期望不同结果。每次调用都往 messages 列表追加消息，快速消耗 context window。

**核心启示**：循环检测是 Agent 系统的"看门狗"——LLM 不可能 100% 自我纠错。代码层面必须有硬机制检测和打断重复行为。两级响应（先警告、后硬停）给 LLM 自我纠正的机会，但也保证最坏情况下系统不会无限循环。

## 5. TokenUsageMiddleware——用量可观测

```python
# 每次 LLM 调用后记录
input_tokens = usage_metadata.get("input_tokens", 0)
output_tokens = usage_metadata.get("output_tokens", 0)
logger.info(f"Token usage: input={input_tokens}, output={output_tokens}")
```

纯观测中间件，不做任何状态修改。用于：
- 监控每次对话的 token 消耗趋势
- 判断 SummarizationMiddleware 的触发阈值是否合理
- 成本估算（input/output token 价格不同）

**核心启示**：可观测性是优化的前提。不知道每次对话消耗多少 token，就无法判断上下文管理策略是否有效。TokenUsageMiddleware 是最轻量的实现——只读 `usage_metadata`、只打日志——但提供了最重要的基础数据。
