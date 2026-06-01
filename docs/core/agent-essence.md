# 各模块本质

## 配置（config）

配置系统是 DeerFlow 的**统一声明层**——从两个文件（`config.yaml` 静态配置 + `extensions_config.json` 动态配置）加载、校验、解析环境变量，通过 ContextVar 覆盖栈和 mtime 缓存实现协程安全的配置热更新，为所有子系统提供配置基座。

## 反射（reflection）

反射系统是**"字符串路径到 Python 对象"的动态解析器**——通过 `resolve_variable()` / `resolve_class()` 把 `config.yaml` 中的 `"module.path:ClassName"` 字符串动态导入并验证类型，是配置驱动架构的基础，让模型工厂、工具加载、沙箱 Provider 等所有可插拔组件都无需硬编码。

## 持久化（persistence）

持久化层是**应用数据的统一存储抽象**——用 SQLAlchemy 异步 ORM 管理 Run、RunEvent、ThreadMeta、Feedback、User 五大实体，支持 memory/SQLite/PostgreSQL 三后端切换，通过 `json_compat` 编译扩展抹平跨方言 JSON 查询差异，与 LangGraph Checkpointer 完全独立。

## 模型系统（models）

模型系统是一个**"声明式配置到可调用实例"的反射工厂**——通过 `use` 字段动态加载 Provider 类，把 `config.yaml` 里的声明式配置转化为统一的 `BaseChatModel` 实例，让上层 Agent 无需关心不同 LLM Provider 在认证、流式、thinking 上的差异。

## 工具系统（tools）

工具系统是一条**按优先级装配的工具管线**——按"配置工具 > 内置工具 > MCP 工具 > ACP 工具"的顺序收集、过滤、去重，最终生成 Agent 可调用的 `BaseTool` 列表，支持按组过滤和延迟加载（tool_search）。

## MCP

MCP 是**外部工具的缓存加载层**——通过 mtime 比对实现零开销的配置热更新检测，将 MCP 服务器的工具发现结果全局缓存，避免每次 Agent 调用都重新启动子进程或建立网络连接。

## 技能（skills）

技能系统是**Agent 的按需加载知识包机制**——每个技能是一个 `SKILL.md` + 辅助文件，仅名称和描述注入 system prompt，Agent 在需要时才通过 `read_file` 读取完整内容，支持内置/自定义/Agent 自创三种来源和 LLM 安全扫描。

## 记忆（memory）

记忆系统是**跨会话的用户画像自动提取与注入系统**——通过"存储 → LLM 提取 → 注入 → 防抖队列"四层架构，自动从对话中提取用户偏好、知识背景、行为模式等 facts，按置信度排序截断后注入下一轮 system prompt，实现 Agent 的个性化。

## 子代理（subagents）

子代理系统是**主代理的任务委派与并行执行机制**——通过 `task()` 工具将子任务提交到双线程池架构（调度池 + 持久化事件循环），最多 3 个子代理并行执行，SSE 事件实时推送进度，Token 用量回收到父代理统计。

## 沙箱（sandbox）

沙箱系统是**Agent 工具执行的隔离环境抽象层**——通过统一的虚拟路径接口（`/mnt/user-data/`）和 Provider 模式（本地文件系统 / Docker 容器），让 Agent 工具代码无需关心底层是本地还是容器，配合路径验证、输出屏蔽、命令审计构成多层安全防线。

## 上传（uploads）

上传系统是**文件上传的纯安全逻辑层**——负责文件名规范化、路径遍历防护、符号链接攻击防御（POSIX `O_NOFOLLOW` / Windows 双重 lstat）、文件名冲突处理，与 FastAPI 解耦以同时服务 Gateway API 和嵌入式 DeerFlowClient。

## Guardrails（护栏）

Guardrails 是**工具调用的前置策略授权层**——在每次工具执行前插入"安检口"，由可插拔的 Provider（内置 Allowlist / OAP 护照 / 自定义）评估 allow/deny 决策，是沙箱进程隔离的语义级补充，构成纵深防御的第二道防线。

## Channels（IM 通道）

Channels 是**多平台 IM 接入层**——通过 MessageBus 发布/订阅模式解耦频道与调度器，支持飞书、钉钉、企微、Slack、Discord、Telegram 等平台，所有连接采用 WebSocket 或长轮询的出站方式，无需暴露公网端口。

## Gateway

Gateway 是**DeerFlow 的统一入口**——基于 FastAPI 构建，同时承担 REST API 网关和嵌入式 LangGraph 运行时双重职责，通过 Nginx 反向代理对外暴露，请求经过 CORS → CSRF → JWT 认证 → 权限检查的完整安全链。

## 运行时（runtime）

运行时是**Agent 图的后台执行与事件推送系统**——RunManager 维护内存注册表追踪运行状态，StreamBridge 通过有界缓冲区连接后台 Agent 执行与前端 SSE，RunJournal 通过 LangChain 回调无侵入地记录 LLM 调用和工具执行的完整事件流。

## 追踪（tracing）

追踪系统是**Agent 运行的可观测性桥梁**——通过统一的 `build_tracing_callbacks()` 入口，将 LangSmith 和 Langfuse 两个追踪平台的回调按需创建并注入 Agent 运行时，延迟导入确保未启用时零开销。

## 前端（frontend）

前端是**SSE 流式驱动的消息渲染系统**——通过 `useStream` hook 接收实时事件，三源合并（历史 + 流 + 乐观消息）后按消息类型分组渲染，支持流式 Markdown、推理内容折叠、子代理进度卡片，历史回放与实时流统一到同一渲染管线。

## 社区工具（community）

社区工具是**可选的扩展工具集**——提供搜索引擎（Tavily/DuckDuckGo/Serper/Exa）、网页抓取（Jina AI/Firecrawl）、Docker 沙箱（AioSandbox）等扩展能力，所有工具均为可选依赖，通过 config.yaml 按需加载，未安装时优雅降级。

## 工具函数（utils）

工具函数是**无业务依赖的纯基础设施层**——提供文档转 Markdown（PDF 双转换器策略）、线程安全端口分配、网页内容提取（HTML → Article → Markdown）、ISO 8601 时间戳标准化四个独立工具模块，各模块无交叉依赖。

## Agent

Agent 是一个**以中间件链驱动的 LLM 调用编排器**——把模型、工具、技能、子代理五种能力通过固定槽位的中间件管道串联起来，在 LangGraph 图中完成"接收请求 → 注入上下文 → 调用 LLM → 执行工具 → 返回结果"的循环。
