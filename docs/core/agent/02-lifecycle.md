# Agent 完整生命周期

本文档描述 DeerFlow Agent 从创建到执行的完整生命周期，包括模块初始化、Agent 构建、请求处理和状态管理。

## 1. 模块初始化阶段

当 `deerflow.agents` 包被 LangGraph 导入注册图时触发：

```
import deerflow.agents
  → __init__.py 模块级代码执行
    → prime_enabled_skills_cache()        启动后台线程预热技能缓存
    → 导出公共 API（create_deerflow_agent, RuntimeFeatures, make_lead_agent 等）
```

技能缓存预热是**非阻塞**的 — 后台线程加载技能文件，请求路径读取热缓存，避免同步文件 I/O 阻塞。

## 2. Agent 构建阶段

LangGraph Server 调用 `make_lead_agent(config: RunnableConfig)` 构建 Agent 图：

### 2.1 运行时配置解析

```
make_lead_agent(config)
  → _get_runtime_config(config)
    → 合并 config["configurable"] + config["context"]
    → 提取参数：thinking_enabled, model_name, is_plan_mode, subagent_enabled 等
```

### 2.2 模型解析

```
_resolve_model_name(requested_name)
  → 优先级：请求参数 model_name → Agent 配置 model → 全局默认模型
  → 无效模型名自动回退到默认，打印警告
```

### 2.3 中间件链组装

```
_build_middlewares(config, model_name, agent_name)
  → build_lead_runtime_middlewares()     基础运行时中间件
    → ThreadDataMiddleware               创建线程目录
    → UploadsMiddleware                  注入上传文件
    → SandboxMiddleware                  配置沙箱环境
    → DanglingToolCallMiddleware         修补悬挂调用
    → LLMErrorHandlingMiddleware         LLM 错误重试 + 熔断
    → GuardrailMiddleware (可选)          安全护栏
    → SandboxAuditMiddleware             Bash 安全审计
    → ToolErrorHandlingMiddleware        工具异常处理
  → DynamicContextMiddleware             记忆/日期动态注入
  → SummarizationMiddleware (可选)       对话摘要压缩
  → TodoMiddleware (plan_mode)           任务追踪
  → TokenUsageMiddleware                 Token 用量统计
  → TitleMiddleware                      自动标题生成
  → MemoryMiddleware                    记忆更新排队
  → ViewImageMiddleware (vision 模型)    图像注入
  → DeferredToolFilterMiddleware (可选)  延迟工具过滤
  → SubagentLimitMiddleware (可选)       子代理并发限制
  → LoopDetectionMiddleware              循环检测
  → ClarificationMiddleware              澄清拦截（始终最后）
```

### 2.4 系统提示词构建

```
apply_prompt_template(subagent_enabled, agent_name, available_skills)
  → 加载 Agent SOUL.md（自定义 Agent 个性）
  → 生成技能列表段落（带渐进式加载说明）
  → 构建子代理系统提示（并发限制 + 多批次策略）
  → 注入 ACP Agent 路径（如有配置）
  → 返回完全静态的系统提示词（最大化前缀缓存命中率）
```

### 2.5 工具加载

```
get_available_tools(groups, include_mcp, model_name, subagent_enabled)
  → 配置定义工具（config.yaml → resolve_variable）
  → MCP 工具（懒加载，带 mtime 缓存失效）
  → 内置工具（bash, ls, read_file, write_file, str_replace, present_files 等）
  → 子代理工具（task，如启用）
  → 工具过滤（filter_tools_by_skill_allowed_tools）
```

### 2.6 图构建

```
create_agent(model, tools, middleware, system_prompt, state_schema=ThreadState)
  → LangChain create_agent 原语
  → 返回 CompiledStateGraph（LangGraph 编译后的图）
```

## 3. 请求处理阶段

每次用户发送消息时，LangGraph 驱动 Agent 执行循环：

### 3.1 Agent 循环

```
用户消息
  → LangGraph 将消息追加到 ThreadState.messages
  → Agent 循环开始：
    ┌─────────────────────────────────────────┐
    │ 1. 中间件 before_agent                  │
    │    → DynamicContext 注入记忆/日期         │
    │    → TodoMiddleware 清理其他 run 提醒     │
    │                                         │
    │ 2. 中间件 before_model                  │
    │    → SummarizationMiddleware 检查摘要     │
    │    → TodoMiddleware 检查上下文丢失         │
    │    → ViewImageMiddleware 注入图像         │
    │                                         │
    │ 3. 中间件 wrap_model_call               │
    │    → DanglingToolCallMiddleware 修补消息  │
    │    → LLMErrorHandlingMiddleware 重试/熔断│
    │    → DeferredToolFilterMiddleware 过滤   │
    │    → TodoMiddleware 注入完成提醒          │
    │    → 实际 LLM 调用                       │
    │                                         │
    │ 4. 中间件 after_model                   │
    │    → TokenUsageMiddleware 统计 token     │
    │    → TitleMiddleware 生成标题             │
    │    → SubagentLimitMiddleware 截断多余调用 │
    │    → LoopDetectionMiddleware 循环检测     │
    │    → TodoMiddleware 检查未完成任务         │
    │                                         │
    │ 5. 如果有 tool_calls → 执行工具          │
    │    → 中间件 wrap_tool_call               │
    │      → SandboxAuditMiddleware 安全审计   │
    │      → ToolErrorHandlingMiddleware 异常处理│
    │      → ClarificationMiddleware 澄清拦截  │
    │      → 实际工具执行                      │
    │                                         │
    │ 6. 如果无 tool_calls → 返回最终响应      │
    │    → 中间件 after_agent                  │
    │      → MemoryMiddleware 排队记忆更新      │
    │      → TodoMiddleware 清理提醒            │
    └─────────────────────────────────────────┘
```

### 3.2 中间件执行顺序

中间件按固定顺序串联，每个中间件在特定的生命周期钩子中执行：

```
before_agent → before_model → wrap_model_call → after_model → wrap_tool_call → after_agent
```

所有中间件同时提供同步和异步版本（`before_agent` / `abefore_agent`）。

## 4. 特殊路径

### 4.1 Bootstrap Agent

当 `is_bootstrap=True` 时，创建最小化 Agent：
- 仅暴露 `setup_agent` 工具
- 最简提示词
- 不加载技能/子代理

### 4.2 自定义 Agent

当 `agent_name` 非 None 时：
- 加载 Agent 专属 `SOUL.md` 个性描述
- 暴露 `update_agent` 工具（自我更新 SOUL.md/config.yaml）
- 使用 per-agent 记忆存储
- 按配置白名单过滤技能

### 4.3 用户中断恢复

当用户中断导致工具调用缺少 ToolMessage 时：
- `DanglingToolCallMiddleware` 检测并注入合成错误响应
- 支持三种来源：`tool_calls`、`additional_kwargs["tool_calls"]`、`invalid_tool_calls`

## 5. 状态持久化

### 5.1 检查点（Checkpointer）

LangGraph 通过 `BaseCheckpointSaver` 管理 ThreadState 持久化：
- 每个线程独立状态
- 支持中断/恢复

### 5.2 记忆持久化

```
MemoryMiddleware.after_agent
  → 过滤消息（仅保留 user + final AI）
  → 捕获 user_id（ContextVar 在 Timer 线程不可用）
  → 排队到 MemoryQueue（防抖 30s）
    → 后台 LLM 提取事实/更新上下文
    → 原子文件写入（temp + rename）
    → 缓存失效
```

### 5.3 技能缓存

```
模块导入时 → prime_enabled_skills_cache()
  → 后台线程加载
  → 版本化失效（_enabled_skills_refresh_version 递增时重新加载）
  → per-config 缓存（按 AppConfig 身份缓存）
  → LRU 缓存（格式化后的技能段落）
```
