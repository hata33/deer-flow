# 架构决策记录（ADR）— 索引

> 本文档是 DeerFlow 架构决策记录的总索引。每个模块的详细决策记录和实现分析位于各自的 `docs/core/{module}/` 目录下。

---

## 决策记录索引

| 模块 | 设计决策 | 实现分析 | 核心决策 |
|------|---------|---------|---------|
| **Agent 系统** | [06-design-decisions.md](../core/agent/06-design-decisions.md) | [07-implementation-analysis.md](../core/agent/07-implementation-analysis.md) | 中间件链 vs 继承、静态提示词 + ID-swap、LangGraph ReAct |
| **配置系统** | [06-design-decisions.md](../core/config/06-design-decisions.md) | [07-implementation-analysis.md](../core/config/07-implementation-analysis.md) | YAML + Pydantic、mtime 热重载、$ENV_VAR 解析 |
| **模型工厂** | [04-design-decisions.md](../core/models/04-design-decisions.md) | [05-implementation-analysis.md](../core/models/05-implementation-analysis.md) | 反射工厂、thinking 覆盖、vLLM reasoning 字段保留 |
| **记忆系统** | [002-design-decisions.md](../core/memory/002-design-decisions.md) | [003-implementation-analysis.md](../core/memory/003-implementation-analysis.md) | 构建时注入、JSON + ABC、Facts+Sections 双轨、防抖 30s |
| **技能系统** | [05-design-decisions.md](../core/skills/05-design-decisions.md) | [06-implementation-analysis.md](../core/skills/06-implementation-analysis.md) | allowed-tools 并集策略、只读沙箱挂载、静态提示词注入 |
| **子代理** | [05-design-decisions.md](../core/subagents/05-design-decisions.md) | [06-implementation-analysis.md](../core/subagents/06-implementation-analysis.md) | 双线程池 vs asyncio、MAX_CONCURRENT=3、15 分钟超时 |
| **沙箱** | [05-design-decisions.md](../core/sandbox/05-design-decisions.md) | [06-implementation-analysis.md](../core/sandbox/06-implementation-analysis.md) | 虚拟路径 /mnt/ 抽象、PathMapping + LRU 缓存 |
| **工具系统** | [04-design-decisions.md](../core/tools/04-design-decisions.md) | [05-implementation-analysis.md](../core/tools/05-implementation-analysis.md) | @tool + parse_docstring、反射加载、tool groups |
| **MCP** | [06-design-decisions.md](../core/mcp/06-design-decisions.md) | [07-implementation-analysis.md](../core/mcp/07-implementation-analysis.md) | 懒初始化 + mtime 缓存、三种 transport、OAuth 自动刷新 |
| **社区工具** | [05-design-decisions.md](../core/community/05-design-decisions.md) | [06-implementation-analysis.md](../core/community/06-implementation-analysis.md) | @tool 模式、标准化输出格式、双引擎 PDF |
| **Gateway** | [05-design-decisions.md](../core/gateway/05-design-decisions.md) | [06-implementation-analysis.md](../core/gateway/06-implementation-analysis.md) | FastAPI + 嵌入式 LangGraph、CORS/CSRF、SSE 心跳 |
| **IM 频道** | [05-design-decisions.md](../core/channels/05-design-decisions.md) | [06-implementation-analysis.md](../core/channels/06-implementation-analysis.md) | MessageBus pub/sub、per-platform 流式策略 |
| **Guardrails** | [05-design-decisions.md](../core/guardrails/05-design-decisions.md) | [06-implementation-analysis.md](../core/guardrails/06-implementation-analysis.md) | Protocol-based Provider、fail_closed 默认值 |
| **运行时** | [09-design-decisions.md](../core/runtime/09-design-decisions.md) | [10-implementation-analysis.md](../core/runtime/10-implementation-analysis.md) | StreamBridge 抽象基类、RunManager 并发策略 |
| **持久化** | [08-design-decisions.md](../core/persistence/08-design-decisions.md) | [09-implementation-analysis.md](../core/persistence/09-implementation-analysis.md) | RunStore 抽象接口、per-user 隔离 |
| **文件上传** | [01-design-decisions.md](../core/uploads/01-design-decisions.md) | [02-implementation-analysis.md](../core/uploads/02-implementation-analysis.md) | markitdown 转换、路径穿越防护、记忆清洗 |
| **反射** | [01-design-decisions.md](../core/reflection/01-design-decisions.md) | [02-implementation-analysis.md](../core/reflection/02-implementation-analysis.md) | module:variable 约定、可操作安装提示 |
| **追踪** | [01-design-decisions.md](../core/tracing/01-design-decisions.md) | [02-implementation-analysis.md](../core/tracing/02-implementation-analysis.md) | 结构化事件、按需启用、LangSmith 兼容 |
| **工具类** | [01-design-decisions.md](../core/utils/01-design-decisions.md) | [02-implementation-analysis.md](../core/utils/02-implementation-analysis.md) | Readability 正文提取、PDF 双引擎、文件名安全 |

---

## 按主题查找

### Agent 编排

| 主题 | 文档 | 关键决策 |
|------|------|---------|
| 中间件链设计 | Agent 06 | 组合优于继承，6 个生命周期钩子 |
| 静态提示词 + ID-swap | Agent 06 | prefix cache 复用 vs 实时性 |
| 错误转 ToolMessage | Agent 06 | Agent 自主决策 vs 系统终止 |
| LangGraph 选择 | Agent 06 | 框架能力 vs 手写控制力 |

### 执行基础设施

| 主题 | 文档 | 关键决策 |
|------|------|---------|
| 双线程池 | Subagents 05 | 同步上下文中的异步执行 |
| 虚拟路径 | Sandbox 05 | 环境无关的文件访问 |
| 配置驱动 | Config 06 | 反射加载 vs 硬编码 |

### 数据与记忆

| 主题 | 文档 | 关键决策 |
|------|------|---------|
| 防抖队列 | Memory 002 | threading.Timer vs asyncio |
| LLM 驱动更新 | Memory 002 | 规则提取 vs LLM 理解 |
| Facts + Sections | Memory 002 | 精确可排序 vs 叙事上下文 |

### 扩展与集成

| 主题 | 文档 | 关键决策 |
|------|------|---------|
| @tool 模式 | Community 05 | LangChain 标准工具注册 |
| MCP 懒加载 | MCP 06 | 首次使用时加载 + mtime 缓存 |
| Guardrail Protocol | Guardrails 05 | Protocol vs ABC |
