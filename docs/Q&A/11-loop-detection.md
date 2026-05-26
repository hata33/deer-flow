# Q&A 11: 检测并打断循环处理

> 检测并打断循环处理，是针对什么场景设计的？打断的条件和机制是什么？

---

## 针对的场景

`LoopDetectionMiddleware` 针对 **Agent 的工具调用死循环**——LLM 反复调用相同的工具和参数，无法前进。

### 典型循环模式

```
第 1 轮: AIMessage(tool_calls=[bash("ls /tmp")])
         ToolMessage: "file1.txt  file2.txt"

第 2 轮: AIMessage(tool_calls=[bash("ls /tmp")])      ← 完全相同
         ToolMessage: "file1.txt  file2.txt"

第 3 轮: AIMessage(tool_calls=[bash("ls /tmp")])      ← 还是相同
         ToolMessage: "file1.txt  file2.txt"

... 无限重复 ...
```

**原因**: LLM 可能"陷入"某个工具调用模式，尤其当工具返回的信息不够明确时。

---

## 两种检测机制

### 1. Hash-based 检测（相同调用集）

检测**完全相同的工具调用集合**在滑动窗口内重复出现。

```
滑动窗口: 最近 20 次 tool_calls

每次 LLM 返回 tool_calls → 计算所有调用的 hash
                             (tool_name + normalized_args)

hash 在窗口内出现次数 ≥ warn_threshold → 警告
hash 在窗口内出现次数 ≥ hard_stop_threshold → 强制停止
```

| 参数 | 默认值 | 含义 |
|------|-------|------|
| 窗口大小 | 20 | 追踪最近 20 次调用 |
| `warn_threshold` | 3 | 注入警告消息 |
| `hard_stop_threshold` | 5 | 强制移除所有 tool_calls |

### 2. Frequency-based 检测（单工具频率）

检测**单个工具类型**被调用的总次数，无论参数是否相同。

```
工具调用计数:
    bash: 32 次
    read_file: 5 次
    write_file: 2 次

bash ≥ tool_freq_warn (30) → 警告
bash ≥ tool_freq_hard_stop (50) → 强制停止
```

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `tool_freq_warn` | 30 | 单工具调用警告阈值 |
| `tool_freq_hard_stop` | 50 | 单工具调用强制停止阈值 |

**支持按工具覆盖**: `bash` 工具天然高频，可以单独设置更高的阈值。

---

## 打断机制

### 阶段一：警告（warn_threshold）

当检测到重复模式达到 3 次时：

```python
# 在现有 AIMessage 的 content 后追加警告文本
warning_text = (
    "[LOOP WARNING] You have made identical tool calls "
    "multiple times. Consider using a different approach."
)
# 追加到当前 AIMessage（而非插入新消息）
last_ai_message.content += warning_text
```

**为什么追加到 AIMessage 而非插入新消息**: LangGraph 要求 AIMessage 和 ToolMessage 严格配对。如果插入新消息，会导致 tool_calls 和 ToolMessage 不匹配。

### 阶段二：强制停止（hard_stop_threshold）

当重复达到 5 次时：

```python
# 1. 移除所有 tool_calls
last_ai_message.tool_calls = []
# 2. 设置 finish_reason
last_ai_message.finish_reason = "stop"
# 3. 注入停止消息
last_ai_message.content += (
    "[FORCED STOP] Repeated tool calls exceeded the safety limit. "
    "Producing final answer with results collected so far."
)
```

**效果**: LLM 被迫以纯文本回复（不调用工具），基于已收集的信息给出最终答案。

---

## 线程级追踪

循环检测是**按线程独立追踪**的：

```python
# 每个线程独立的追踪状态
_per_thread_tracking = {}  # {thread_id: LoopTracker}
```

**LRU 驱逐**: 最多追踪 100 个线程，超出后驱逐最早访问的。

**线程安全**: 使用 `threading.Lock` 保护共享状态。

---

## 参数归一化

不同工具的参数归一化策略不同：

| 工具类型 | 归一化策略 | 原因 |
|---------|-----------|------|
| bash | 命令文本直接 hash | 命令完全相同才是重复 |
| read_file | 路径归一化后 hash | `./file.txt` 和 `file.txt` 是同一个文件 |
| 其他 | JSON 序列化后 hash | 通用策略 |

---

## 检测流程总结

```
LLM 返回 AIMessage(tool_calls=[...])
    │
    ▼
after_model 钩子:
    │
    ├── 计算 tool_calls hash
    ├── 更新滑动窗口
    │
    ├── Hash 检测
    │   ├── count < 3 → 不处理
    │   ├── count = 3~4 → 追加警告文本
    │   └── count ≥ 5 → 移除所有 tool_calls，强制文本回复
    │
    └── Frequency 检测
        ├── count < 30 → 不处理
        ├── count = 30~49 → 追加警告文本
        └── count ≥ 50 → 移除所有 tool_calls，强制文本回复
```

---

## 与 recursion_limit 的区别

| 维度 | LoopDetectionMiddleware | recursion_limit |
|------|------------------------|-----------------|
| **检测对象** | 重复的工具调用模式 | ReAct 循环总迭代次数 |
| **触发原因** | 死循环 | 超时/过长 |
| **阈值** | 3~5 次重复 | 1000 次迭代 |
| **策略** | 保留已收集信息，强制文本回复 | 直接终止运行 |
| **粒度** | 细粒度（可恢复） | 粗粒度（不可恢复） |

`LoopDetectionMiddleware` 是更早、更温和的干预。`recursion_limit` 是最后的硬防线。

---

## 相关源码

| 组件 | 文件 |
|------|------|
| 循环检测中间件 | `backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py` |
| 子代理限制中间件 | `backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py` |
| Agent 配置 | `backend/packages/harness/deerflow/config/app_config.py` |

## 深入阅读

- [Agent 设计决策](../docs/core/agent/06-design-decisions.md)
- [Agent 请求全流程](../docs/lifecycle/01-agent-request-flow.md)
