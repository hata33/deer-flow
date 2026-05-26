# 02 - 实现机制深度分析

> 本文档基于源码逐层拆解追踪系统的实现细节。

---

## 一、追踪事件类型

系统在关键节点发出结构化事件：

| 事件 | 触发时机 | 包含字段 |
|------|---------|---------|
| llm_start | LLM 调用前 | model, messages, run_id |
| llm_end | LLM 调用后 | response, usage, latency |
| tool_start | 工具调用前 | tool_name, args, run_id |
| tool_end | 工具调用后 | result, latency |
| middleware | 中间件执行 | middleware_name, hook, decision |

---

## 二、追踪与中间件的协作

追踪数据部分由中间件产生：

- **TokenUsageMiddleware**: 记录每次 LLM 调用的 token 用量
- **RunJournal**: 记录 run 级别的事件（run_start, run_end, error）

---

## 三、与 LangSmith 的集成

通过 LangChain 回调机制自动集成：
- `on_llm_start` / `on_llm_end` → LangSmith trace
- `on_tool_start` / `on_tool_end` → LangSmith span
- 中间件 tagged 调用（如 `middleware:summarize`）可区分主循环和中间件 LLM 调用
