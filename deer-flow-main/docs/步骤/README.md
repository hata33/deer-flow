# DeerFlow 步骤文档索引

从用户请求到 agent 执行完毕的完整流程文档，按顺序阅读可理解整个系统。

---

## 步骤目录

| 序号 | 文件 | 标题 | 一句话摘要 |
|------|------|------|-----------|
| 01 | [01-核心流程概览](01-核心流程概览.md) | 核心流程概览 | 请求→构建→流式执行→事件推送的完整管道，含模块地图和 Mermaid 流程图 |
| 02 | [02-整体架构分层](02-整体架构分层.md) | 整体架构 | 从用户请求到 agent 执行完毕，按路由/Service/运行时/工厂/配置/Bridge/推流七层梳理 |
| 03 | [03-全局单例初始化](03-全局单例初始化.md) | 全局单例初始化 | app 启动时在 `app.state` 上创建 bridge、checkpointer、store、run_manager 四个单例 |
| 04 | [04-StreamBridge发布订阅](04-StreamBridge发布订阅.md) | StreamBridge 发布订阅 | 抽象基类 + asyncio.Queue 内存实现，按 `run_id` 分发，生产者消费者解耦 |
| 05 | [05-RunRecord的作用](05-RunRecord的作用.md) | RunRecord 的作用 | 每次请求创建唯一 `run_id`，绑定 agent 执行与 SSE 推流，一次性信封 |
| 06 | [06-路由层请求处理](06-路由层请求处理.md) | 路由层请求处理 | `/{thread_id}/runs/stream` 路由如何获取依赖、调用 `start_run`、返回 SSE |
| 07 | [07-run_agent与推流协作](07-run_agent与推流协作.md) | run_agent 与推流协作 | astream chunk → serialize → bridge.publish → sse_consumer 的完整推流链路 |
| 08 | [08-run_agent执行核心](08-run_agent执行核心.md) | run_agent 执行核心 | `asyncio.create_task(run_agent(...))` 后的 agent 实际执行逻辑 |
| 09 | [09-agent初始化链路](09-agent初始化链路.md) | agent 初始化链路 | `agent_factory(config)` 触发 `make_lead_agent` 的整套初始化 |
| 10 | [10-全局配置加载链路](10-全局配置加载链路.md) | 全局配置加载 | config.yaml 从文件读取到子模块分发解析的完整链路 |
| 11 | [11-agent参数来源](11-agent参数来源.md) | agent 参数来源 | body 运行时参数 + config.yaml 静态配置的合并机制 |
| 12 | [12-make_lead_agent工厂](12-make_lead_agent工厂.md) | make_lead_agent 工厂 | 准备 model/tools/middleware/prompt/state_schema 五大传参调用 `create_agent()` |
| 13 | [13-astream执行编排](13-astream执行编排.md) | astream 执行编排 | `agent.astream()` 的执行编排：stream_mode 映射、取消检测、状态管理 |
| 14 | [14-核心执行与推流链路](14-核心执行与推流链路.md) | 核心执行与推流 | `agent.astream()` 这一行代码前后的分界——准备参数 → 消费结果 |
| 15 | [15-ReAct循环内部执行](15-ReAct循环内部执行.md) | ReAct 循环内部 | 中间件在各阶段的介入、工具调用分发、ThreadState 状态流转 |
| 16 | [16-工具调用链路](16-工具调用链路.md) | 工具调用链路 | LLM 产出 `tool_calls` 后，经过中间件拦截、实际执行、结果回传、推流通知前端的完整链路 |
| 17 | [17-运行后副作用](17-运行后副作用.md) | 运行后副作用 | 标题生成、记忆更新、线程数据清理三个异步副作用 |
| 18 | [18-反射与配置驱动设计](18-反射与配置驱动设计.md) | 反射与配置驱动设计 | 字符串路径 + 运行时反射实现配置驱动：改 YAML 切换模型/工具/沙箱 |

## Agent 子系统（agent/ 目录）

| 文件 | 标题 | 一句话摘要 |
|------|------|-----------|
| [agent/001-agent构建与编排](agent/001-agent构建与编排.md) | Agent 构建与编排 | `make_lead_agent()` 到可执行编译图的完整流程 |
| [agent/002-工厂函数与模型解析](agent/002-工厂函数与模型解析.md) | 工厂函数与模型解析 | 五步组装 + 三级模型优先级（请求 > agent_config > config.yaml） |
| [agent/003-工具加载](agent/003-工具加载.md) | 工具加载 | config 工具 + MCP + 内置 + 条件工具 + 社区工具四层来源 |
| [agent/004-中间件链](agent/004-中间件链.md) | 中间件链 | 14 个中间件的有序链与 before/after_agent/wrap_model/wrap_tool 四种钩子 |
| [agent/005-系统提示词](agent/005-系统提示词.md) | 系统提示词 | 动态模板组装、记忆注入、技能注入、子代理段落 |
| [agent/006-记忆系统](agent/006-记忆系统.md) | 记忆系统 | 文件存储、LLM 更新、防抖队列、注入时机 |
| [agent/007-Checkpointer管理](agent/007-Checkpointer管理.md) | Checkpointer 管理 | 检查点注入时机与 LangGraph 自动管理机制 |
| [agent/008-完整组装时序](agent/008-完整组装时序.md) | 完整组装时序 | HTTP 请求到编译图产出的端到端调用链 |
| [agent/009-lead-agent与subagent协作](agent/009-lead-agent与subagent协作.md) | Lead Agent 与 Subagent 协作 | 编排模式、调用流程、并行控制（最多 3 并发）、双线程池执行 |
| [agent/010-StreamBridge发布订阅模式](agent/010-StreamBridge发布订阅模式.md) | StreamBridge 详细实现 | 抽象接口到内存实现的完整注入链、生产消费时序、哨兵机制 |
| [agent/011-Runtime层与LangGraph-Runtime](agent/011-Runtime层与LangGraph-Runtime.md) | Runtime 层 | Runtime 注入机制、四个子模块职责 |
