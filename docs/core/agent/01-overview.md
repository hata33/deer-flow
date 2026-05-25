# Agent 系统全局概览

DeerFlow Agent 系统是基于 LangGraph 构建的智能体运行时框架，采用**两级工厂 + 声明式特性 + 中间件链**的架构，在单一代码库中同时支持 SDK 级可编程组装和应用级配置驱动两种使用模式。

## 能力来源

Agent 的能力由五个维度组合提供：

| 维度 | 来源模块 | 作用 |
|------|----------|------|
| **模型** | `deerflow.models` | LLM 实例创建，支持 thinking/vision 模式切换 |
| **工具** | `deerflow.tools` + `deerflow.sandbox.tools` + MCP | 沙箱文件操作、bash 命令、内置工具、MCP 远程工具、社区工具 |
| **中间件** | `deerflow.agents.middlewares` (~18 个) | 横切关注点：错误处理、安全审计、循环检测、记忆注入等 |
| **技能** | `deerflow.skills` | 外挂知识文件 (SKILL.md)，按需加载到系统提示词 |
| **子代理** | `deerflow.subagents` | 后台并行执行，支持 general-purpose / bash 等类型 |

## 解决的核心问题

1. **配置复杂性** — 通过 `RuntimeFeatures` 声明式特性标志，一行代码启用/禁用/替换中间件
2. **中间件顺序耦合** — 固定 14 槽位 + `@Next/@Prev` 定位装饰器，消除手动排序错误
3. **提示词缓存** — 系统提示词完全静态，用户相关内容（记忆/日期）通过 `DynamicContextMiddleware` 动态注入
4. **LLM 可靠性** — 瞬态错误重试 + 熔断器 + 工具异常转 ToolMessage，保证对话流不中断
5. **安全边界** — Bash 命令安全审计、循环检测、子代理并发限制、Guardrail 护栏
6. **上下文管理** — 自动摘要压缩、技能 bundle 保护、记忆持久化

## 架构总览

```
agents/
├── __init__.py          ← 包入口 + 技能缓存预热
├── factory.py           ← SDK 级纯参数工厂 create_deerflow_agent()
├── features.py          ← RuntimeFeatures 特性标志 + @Next/@Prev 装饰器
├── thread_state.py      ← ThreadState 状态模式（LangGraph 状态定义）
├── lead_agent/          ← 应用层工厂
│   ├── agent.py         ← make_lead_agent() — 配置驱动的中间件链组装
│   └── prompt.py        ← 系统提示词模板 + 技能缓存管理
├── memory/              ← 跨会话记忆系统
│   ├── prompt.py        ← 注入格式化 + 更新提示词
│   ├── storage.py       ← JSON 文件持久化
│   ├── updater.py       ← LLM 驱动的记忆提取
│   ├── queue.py         ← 防抖队列
│   ├── message_processing.py ← 消息过滤 + 信号检测
│   └── summarization_hook.py ← 摘要前记忆刷入钩子
└── middlewares/          ← 中间件链 (~18 个)
    ├── ThreadDataMiddleware       ← 线程目录管理
    ├── UploadsMiddleware          ← 上传文件注入
    ├── SandboxMiddleware          ← 沙箱执行环境
    ├── DanglingToolCallMiddleware ← 悬挂工具调用修补
    ├── LLMErrorHandlingMiddleware ← LLM 错误重试 + 熔断
    ├── GuardrailMiddleware        ← 安全护栏（可选）
    ├── SandboxAuditMiddleware     ← Bash 命令安全审计
    ├── ToolErrorHandlingMiddleware← 工具异常转 ToolMessage
    ├── DynamicContextMiddleware   ← 记忆/日期动态注入
    ├── SummarizationMiddleware    ← 对话摘要压缩
    ├── TodoMiddleware             ← 任务追踪 + 防提前退出
    ├── TokenUsageMiddleware       ← Token 用量统计 + 步骤归属
    ├── TitleMiddleware            ← 自动标题生成
    ├── MemoryMiddleware           ← 记忆更新排队
    ├── ViewImageMiddleware        ← 图像内容注入
    ├── DeferredToolFilterMiddleware← 延迟工具过滤
    ├── SubagentLimitMiddleware    ← 子代理并发限制
    ├── LoopDetectionMiddleware    ← 循环检测 + 强制停止
    └── ClarificationMiddleware    ← 澄清请求拦截
```

## 两级工厂设计

```
┌─────────────────────────┬────────────────────────────────────────────────────┐
│ 工厂                     │ 特点                                               │
├─────────────────────────┼────────────────────────────────────────────────────┤
│ create_deerflow_agent() │ SDK 级，纯参数，无配置文件依赖，可编程组装            │
│ make_lead_agent()       │ 应用层，配置驱动，读取 config.yaml，供 LangGraph 调用 │
└─────────────────────────┴────────────────────────────────────────────────────┘
```

- **`create_deerflow_agent()`** — 接受 `RuntimeFeatures` + `extra_middleware`，通过 `_assemble_from_features()` 自动组装中间件链
- **`make_lead_agent()`** — 从 `RunnableConfig` 解析运行时参数，调用 `_build_middlewares()` 组装完整 Lead Agent

## 核心执行流程

```
用户请求
  → LangGraph Server 调用 make_lead_agent(config)
    → _get_runtime_config()          合并 configurable + context
    → _resolve_model_name()          请求模型 → Agent 配置模型 → 全局默认
    → _build_middlewares()           组装中间件链 (~18 个)
    → apply_prompt_template()        构建系统提示词（静态 + 技能列表）
    → create_agent()                 调用 LangChain create_agent 构建图
  → 中间件链执行
    → before_agent                   DynamicContext 注入记忆/日期
    → wrap_model_call                LLM 调用 + 错误重试 + 熔断
    → after_model                    Token 统计 / 标题生成 / 循环检测
    → wrap_tool_call                 安全审计 / 工具异常处理 / 澄清拦截
    → after_agent                    记忆排队
  → 返回结果
```

## 状态管理

`ThreadState` 继承自 LangChain `AgentState`，定义了 Agent 运行时的状态结构：

| 字段 | 类型 | 说明 |
|------|------|------|
| `sandbox` | `SandboxState` | 沙箱 ID |
| `thread_data` | `ThreadDataState` | 线程数据路径 |
| `title` | `str` | 自动生成的对话标题 |
| `artifacts` | `list[str]` | 产出物路径（带去重 reducer） |
| `todos` | `list` | 任务追踪列表 |
| `uploaded_files` | `list[dict]` | 上传文件元信息 |
| `viewed_images` | `dict[str, ViewedImageData]` | 已查看图像（带合并 reducer） |
