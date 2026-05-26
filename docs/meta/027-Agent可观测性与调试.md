# Agent 可观测性与调试

**问题**: Agent 的执行路径不可预测——LLM 自己决定调什么工具、怎么推理。出了问题你不知道"它做了什么"和"为什么这么做"。传统日志看不懂 Agent 行为。

---

## 问题 1：为什么 Agent 系统需要专门的可观测性？

传统 Web 系统：
```
请求 → 路由 → 控制器 → 数据库 → 响应
路径确定，日志可预测
```

Agent 系统：
```
请求 → Agent → LLM 决策 → 工具 A（或 B 或 C?）
                         → 再问 LLM → 工具 D（或 E?）
                         → 循环? 停止?
路径不确定，传统日志无法回答"为什么选了工具 A"
```

需要记录：LLM 的输入/输出、工具调用/结果、中间件决策、token 消耗。

---

## 问题 2：四大观测机制分别看什么？

| 机制 | 看什么 | 给谁看 |
|------|--------|-------|
| StreamBridge | 实时事件流 | 前端用户 |
| RunJournal | 事件审计记录 | 开发者 |
| Checkpointer | 状态快照 | 系统恢复 |
| LangSmith/Langfuse | LLM 调用链 | 成本/质量分析 |

四种机制独立运行，覆盖不同维度。

---

## 问题 3：追踪（Tracing）看什么？

LangSmith/Langfuse 提供的结构化追踪：

```
Run (root trace)
    │
    ├── Span: LLM Call #1
    │   ├── 输入: 500 token（系统提示 400 + 用户消息 100）
    │   ├── 输出: 200 token
    │   ├── 模型: claude-sonnet-4-20250514
    │   ├── 耗时: 2.3s
    │   └── tool_calls: [bash("ls -la")]
    │
    ├── Span: Tool Call - bash
    │   ├── 参数: {"command": "ls -la"}
    │   ├── 结果: "file1.py\nfile2.py"
    │   └── 耗时: 0.1s
    │
    ├── Span: LLM Call #2
    │   ├── 输入: 700 token
    │   └── 输出: 300 token
    │
    └── 总计: 1700 token, 3.5s
```

---

## 问题 4：RunJournal 记录的事件怎么分类？

三类事件：

| 类别 | 内容 | 用途 |
|------|------|------|
| `message` | 用户消息、AI 回复、工具调用/结果 | 对话回放 |
| `trace` | LLM 调用详情、token 统计 | 成本分析 |
| `lifecycle` | Run 启动/完成/中断/错误 | 运行状态 |

```python
# 事件存储
RunEventRow:
    thread_id: str
    run_id: str
    seq: int             # 自增序号
    category: str        # message / trace / lifecycle
    event_type: str      # 具体类型
    data: dict           # 事件数据（JSON）
    timestamp: datetime
```

唯一约束 `(thread_id, seq)` 保证事件严格有序。

---

## 问题 5：如何调试"Agent 为什么做了错误的决策"？

步骤：

```
1. 找到有问题的 Run ID
   → RunRow 表中按时间/用户查询

2. 查看事件流
   → RunEventRow 中按 seq 排序
   → 看每一步的输入/输出

3. 查看追踪链
   → LangSmith/Langfuse 中搜索 Run ID
   → 看每次 LLM 调用的完整 prompt 和 response

4. 定位原因
   → prompt 是否缺少关键信息？
   → 工具返回是否误导了 Agent？
   → 上下文是否被压缩掉了关键内容？

5. 修复
   → 调整 prompt / 增加上下文 / 调整压缩策略
```

---

## 问题 6：Token 消耗怎么追踪？

两级追踪：

**Run 级**（RunRow）：
```python
total_input_tokens: int     # 输入总量
output_tokens: int          # 输出总量
subagent_tokens: int        # 子 Agent 消耗
```

**调用级**（RunJournal 回调）：
```python
# 每次 LLM 调用都记录
on_llm_start: 记录输入 token 数
on_llm_end:   记录输出 token 数 + 模型名 + 耗时
```

归集方式：
```
主 Agent 调用 (1000 token)
    │
    ├── 子 Agent A (500 token) → 归入 subagent_tokens
    └── 子 Agent B (300 token) → 归入 subagent_tokens
    │
总计: 1000 + 500 + 300 = 1800 token
```

---

## 问题 7：追踪对性能有影响吗？

最小化影响：

| 优化 | 效果 |
|------|------|
| 延迟导入 | 不启用时零开销，不导入 SDK |
| 异步写入 | RunJournal 使用缓冲，不阻塞执行 |
| 去重 | 避免同一事件被记录多次 |
| 独立路径 | 追踪失败不影响 Agent 执行 |

```python
# 追踪是装饰性的
try:
    tracing_callback.on_llm_start(...)
except Exception:
    pass  # 追踪失败不影响执行
```

---

## 问题 8：生产环境怎么设置告警？

关键指标和阈值：

| 指标 | 告警条件 | 可能原因 |
|------|---------|---------|
| 单次 Run token | > 50,000 | 死循环或超长对话 |
| Run 失败率 | > 10% | API 不稳定或配置错误 |
| 平均 LLM 耗时 | > 10s | 模型过载 |
| 工具错误率 | > 20% | 工具配置问题 |
| 循环检测触发 | > 0 次/小时 | Prompt 需要优化 |
| 压缩触发频率 | > 5 次/小时 | 对话过长或阈值太低 |

---

## 问题 9：如何回放一次 Run 的完整过程？

```sql
-- 查询某次 Run 的所有事件
SELECT seq, category, event_type, data, timestamp
FROM run_events
WHERE run_id = 'run_abc123'
ORDER BY seq ASC;
```

或通过 LangSmith 的 Trace UI 可视化查看。

回放用途：
- 调试 Agent 行为
- 成本审计
- 质量评估
- 事故回溯

---

## 问题 10：可观测性的完整架构？

```
Agent 执行
    │
    ├── LangChain Callbacks（底层钩子）
    │   ├── RunJournal → 数据库（结构化事件流）
    │   └── Tracing Callback → 追踪平台
    │       ├── LangSmith（云端 SaaS）
    │       └── Langfuse（自部署）
    │
    ├── StreamBridge（实时推送）
    │   └── SSE → 前端实时渲染
    │
    └── Checkpointer（状态持久化）
        └── 数据库 → 状态恢复
    │
    ▼ 消费者
    ├── 开发者: LangSmith + 数据库查询
    ├── 运维: 告警 + Dashboard
    └── 用户: 前端实时进度
```

---

## 数据流概览

```
Agent 执行事件
    │
    ├──→ RunJournal
    │     ├── on_llm_start → 记录输入
    │     ├── on_llm_end   → 记录输出 + token
    │     ├── on_tool_start → 记录参数
    │     └── on_tool_end   → 记录结果
    │     └→ 缓冲 → 批量写入数据库
    │
    ├──→ Tracing (LangSmith/Langfuse)
    │     └→ 结构化 Trace + Span
    │     └→ Dashboard 可视化
    │
    ├──→ StreamBridge
    │     └→ SSE 推送到前端
    │
    └→ Checkpointer
          └→ 状态快照到数据库
```

---

## 源码位置

| 内容 | 文件 |
|------|------|
| RunJournal | `backend/packages/harness/deerflow/runtime/journal.py` |
| StreamBridge | `backend/packages/harness/deerflow/runtime/stream_bridge/base.py` |
| 追踪工厂 | `backend/packages/harness/deerflow/tracing/factory.py` |
| Run 持久化 | `backend/packages/harness/deerflow/persistence/run/` |
| RunEvent 持久化 | `backend/packages/harness/deerflow/persistence/models/run_event.py` |

## 深入阅读

- [追踪概览](../core/tracing/00-overview.md) — 追踪系统
- [流式与持久化](../Q&A/12-langgraph-streaming-persistence.md) — 四大机制对比
- [事件流与持久化](017-事件流与持久化.md) — 持久化详解
- [请求全链路](015-请求全链路.md) — 完整链路追踪
