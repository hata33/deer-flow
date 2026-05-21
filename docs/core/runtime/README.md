# DeerFlow Agent 运行时架构

本目录详细描述了 DeerFlow Agent 运行时的核心架构，从一次用户请求到 Agent 执行完成的完整链路。

## 文档目录

| 文档 | 内容 |
|------|------|
| [01-overview.md](01-overview.md) | 运行时全局概览：组件清单、职责划分、数据流方向 |
| [02-run-lifecycle.md](02-run-lifecycle.md) | 单次运行的完整生命周期：从请求到结束的每一步 |
| [03-capabilities.md](03-capabilities.md) | 运行时使用的各项能力的来源、作用和设计原因 |
| [04-concurrency-control.md](04-concurrency-control.md) | 并发控制：多任务策略（reject/interrupt/rollback）的设计与实现 |
| [05-event-streaming.md](05-event-streaming.md) | 事件流系统：StreamBridge 如何解耦生产者和消费者 |
| [06-event-tracking.md](06-event-tracking.md) | 事件追踪：RunJournal 如何通过回调机制捕获运行全貌 |
| [07-runtime-instances.md](07-runtime-instances.md) | 运行时实例生命周期：RunJournal/Agent 创建策略、多用户并发模型、中断缓存机制 |

## 核心入口

运行时的核心入口是 `run_agent()` 函数（`backend/packages/harness/deerflow/runtime/runs/worker.py:151`），它由 Gateway API 层调用，负责在后台 asyncio Task 中执行 Agent 图，并通过 StreamBridge 将事件实时推送给前端。
