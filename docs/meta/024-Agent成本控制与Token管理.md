# Agent 成本控制与Token管理

**问题**: Agent 一次复杂任务可能消耗数万 token，多用户并发时成本飙升。不加控制的话，一个死循环就能把 API 配额烧光。

---

## 问题 1：Token 消耗在哪些环节？

```
总 Token 消耗
    │
    ├── 主 Agent
    │   ├── 系统提示（每次都发送）    ← 重复消耗
    │   ├── 对话历史（越来越长）      ← 线性增长
    │   ├── 记忆注入                  ← 每次额外增加
    │   └── 工具调用 + 结果           ← 不可预测
    │
    ├── 子 Agent
    │   ├── 子 Agent 系统提示          ← 重复消耗
    │   ├── 子 Agent 工具调用          ← 额外消耗
    │   └── 多个子 Agent 并发          ← 倍增
    │
    └── 压缩摘要
        └── 调用 LLM 生成摘要         ← 额外一次完整调用
```

---

## 问题 2：如何减少系统提示的重复消耗？

**静态提示 + ID 替换（Prefix Cache 优化）**:

```
传统做法:
用户 A → 系统提示(10k token) + 用户上下文 → LLM
用户 B → 系统提示(10k token) + 用户上下文 → LLM
（每个用户的系统提示都是新的，无法利用缓存）

DeerFlow 做法:
构建时: SYSTEM_PROMPT_TEMPLATE = "你是..." （10k token，固定不变）
运行时: 用 user_context 替换 ID 占位符

用户 A → 系统提示(10k, 相同前缀, 缓存命中!) + A 的上下文(200 token)
用户 B → 系统提示(10k, 相同前缀, 缓存命中!) + B 的上下文(200 token)
```

前缀相同 → LLM Provider 的 Prefix Cache 命中 → 输入 token 按缓存价格计费（通常是正常价格的 10%）。

---

## 问题 3：对话历史怎么控制？

三个防线：

| 防线 | 机制 | 效果 |
|------|------|------|
| 压缩中间件 | 超过阈值触发摘要 | 10 万 token → 4 万 token |
| 最近消息保留 | `keep_recent_messages: N` | 保留最近 N 轮，中间的压缩 |
| 注入预算 | 记忆/上下文有 token 上限 | 记忆最多 2000 token |

```yaml
summarization:
  max_tokens: 100000    # 超过此值触发压缩
  keep_recent_messages: 10  # 保留最近 10 轮
memory:
  max_injection_tokens: 2000  # 记忆注入上限
```

---

## 问题 4：子 Agent 的成本怎么控制？

三层限制：

```yaml
subagents:
  max_concurrent: 3         # 最多 3 个并发（防止倍增）
  timeout_seconds: 900      # 15 分钟超时（防止无限运行）
  custom_agents:
    cheap-agent:
      model: "gpt-4o-mini"  # 子 Agent 用便宜模型
```

Token 归集：子 Agent 的所有消耗归入主 Agent，用户看到的是总量：

```python
class TokenCollector:
    """收集子 Agent token，归入主 Agent"""
    def report(self, caller, input_tokens, output_tokens):
        self._totals[caller] += input_tokens + output_tokens
```

---

## 问题 5：不同任务用不同模型怎么配置？

模型分级（Model Tiering）：

```yaml
models:
  default: "claude-sonnet-4-20250514"    # 主 Agent：强模型
  subagents:
    default: "gpt-4o-mini"               # 子 Agent：便宜模型
  summarization:
    model: "gpt-4o-mini"                 # 压缩摘要：便宜模型
  thinking:
    budget_tokens: 6553                   # Thinking 预算自动分配
```

| 任务 | 模型 | 原因 |
|------|------|------|
| 主 Agent 推理 | Claude Sonnet | 需要最强推理能力 |
| 子 Agent 执行 | GPT-4o-mini | 工具调用不需要顶级推理 |
| 压缩摘要 | GPT-4o-mini | 摘要质量要求不高 |
| 循环检测 | 不调用 LLM | 纯算法检测，零成本 |

---

## 问题 6：Thinking Token 怎么控制？

支持 thinking 的模型会自动分配预算：

```python
thinking_budget = int(max_tokens * 0.8)
# 例: max_tokens=8192 → thinking 6553 + 输出 1639
```

为什么 80%: thinking 是内部推理，通常比最终输出长。80/20 是经验值。

用户可以手动限制：

```yaml
providers:
  claude:
    max_tokens: 4096        # 限制总 token
    thinking:
      budget_tokens: 2000   # 限制 thinking token
```

---

## 问题 7：循环检测怎么省钱？

死循环是最大的 token 浪费源。双重检测机制：

```
场景: Agent 重复调用同一工具 50 次
无检测: 50 × (工具调用 + LLM 分析) × ~500 token = ~25,000 token 浪费

有检测:
  第 3 次重复 → 警告（消耗 ~1,500 token）
  第 5 次重复 → 强停（总消耗 ~2,500 token）
  节省: ~22,500 token（90%）
```

---

## 问题 8：如何监控成本？

四层监控：

| 层级 | 工具 | 看什么 |
|------|------|--------|
| Run 级 | RunRow 持久化 | 每次 Run 的 token 总量 |
| Agent 级 | TokenCollector | 主/子 Agent 的 token 分布 |
| 事件级 | RunJournal | 每次 LLM 调用的详细 token |
| 可视化 | LangSmith/Langfuse | 成本趋势、异常检测 |

```python
# RunRow 中的 token 字段
total_input_tokens: int     # 输入总量
output_tokens: int          # 输出总量
subagent_tokens: int        # 子 Agent 消耗
# 总成本 = (total_input_tokens × 输入单价) + (output_tokens × 输出单价)
```

---

## 问题 9：压缩本身不也是成本吗？

是的。生成摘要需要一次额外的 LLM 调用。

```
压缩前: 120 条消息（10 万 token）→ 每轮都发 10 万 token
压缩时: 1 次 LLM 调用（~3000 token 输入 + ~500 token 输出）
压缩后: 41 条消息（4 万 token）→ 每轮只发 4 万 token

ROI 计算:
一次压缩成本: ~3500 token
每轮节省: ~60000 token
第 1 轮就回本: 60000 >> 3500
```

压缩是**高 ROI 投资**——花一次，每轮都省。

---

## 问题 10：生产环境的成本优化清单？

| 优化项 | 预期节省 | 实现难度 |
|--------|---------|---------|
| Prefix Cache（静态提示） | 输入成本降低 ~50% | 低 |
| 模型分级 | 子 Agent 成本降低 ~80% | 低 |
| 上下文压缩 | 长对话成本降低 ~60% | 中 |
| 循环检测 | 避免无限浪费 | 中 |
| 子 Agent 并发限制 | 防止成本倍增 | 低 |
| 记忆注入预算 | 可预测的额外成本 | 低 |
| Thinking 预算 | 控制推理 token | 低 |
| 延迟工具加载 | 避免不必要的 MCP 连接 | 中 |

---

## 数据流概览

```
请求到达
    │
    ▼ 模型分级
主 Agent → Claude Sonnet（强但贵）
子 Agent → GPT-4o-mini（便宜）
压缩    → GPT-4o-mini（便宜）
    │
    ▼ Prefix Cache
静态系统提示（10k token）→ 缓存命中 → 按 10% 计费
    │
    ▼ 上下文压缩
10 万 token → 4 万 token → 节省 60%
    │
    ▼ 循环检测
第 5 次重复 → 强停 → 节省 90%
    │
    ▼ Token 归集
主 Agent + 子 Agent → 总量记录 → 成本监控
```

---

## 源码位置

| 内容 | 文件 |
|------|------|
| 系统提示模板 | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` |
| 模型工厂 | `backend/packages/harness/deerflow/models/factory.py` |
| 压缩中间件 | `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py` |
| Token 归集 | `backend/packages/harness/deerflow/subagents/token_collector.py` |
| Run 持久化 | `backend/packages/harness/deerflow/persistence/run/model.py` |

## 深入阅读

- [上下文压缩中间件](001-上下文压缩中间件.md) — 压缩策略详解
- [循环检测机制](013-循环检测机制.md) — 避免无限浪费
- [模型工厂](016-模型工厂与多Provider.md) — 多模型分级
- [事件流与持久化](017-事件流与持久化.md) — 成本监控数据
