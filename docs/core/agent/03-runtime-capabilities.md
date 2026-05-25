# 运行时能力清单与来源

本文档详细列出 Agent 运行时具备的所有能力，以及每项能力的来源模块和启用条件。

## 模型能力

| 能力 | 来源 | 启用条件 | 配置 |
|------|------|----------|------|
| 基础对话 | `deerflow.models.factory.create_chat_model()` | 始终 | `config.yaml → models[]` |
| 深度思考 | `thinking_enabled=True` | 模型 `supports_thinking: true` | `configurable.thinking_enabled` |
| 视觉理解 | `ViewImageMiddleware` + `view_image_tool` | 模型 `supports_vision: true` | 自动检测 |
| 推理强度调节 | `reasoning_effort` | 模型支持 | `configurable.reasoning_effort` |
| vLLM 思考模式 | `VllmChatModel` | 使用 vLLM 提供者 | `extra_body.chat_template_kwargs.enable_thinking` |

## 工具能力

### 沙箱文件操作

| 工具 | 功能 | 来源 |
|------|------|------|
| `bash` | 执行 Shell 命令 | `deerflow.sandbox.tools` |
| `ls` | 目录树列表（最多 2 层） | `deerflow.sandbox.tools` |
| `read_file` | 读取文件内容（支持行范围） | `deerflow.sandbox.tools` |
| `write_file` | 写入/追加文件 | `deerflow.sandbox.tools` |
| `str_replace` | 子串替换 | `deerflow.sandbox.tools` |

### 内置工具

| 工具 | 功能 | 来源 | 启用条件 |
|------|------|------|----------|
| `present_files` | 展示产出物给用户 | `deerflow.tools.builtins` | 始终 |
| `ask_clarification` | 请求用户澄清 | `deerflow.tools.builtins` | 始终（由 ClarificationMiddleware 拦截） |
| `view_image` | 读取图像为 base64 | `deerflow.tools.builtins` | 模型支持 vision |
| `tool_search` | 搜索并提升延迟工具 | `deerflow.tools.builtins` | `tool_search.enabled` |
| `setup_agent` | 引导创建自定义 Agent | `deerflow.tools.builtins` | `is_bootstrap=True` |
| `update_agent` | 自我更新 SOUL.md/config | `deerflow.tools.builtins` | 自定义 Agent |
| `task` | 委派子代理执行 | `deerflow.tools.builtins` | `subagent_enabled=True` |

### MCP 远程工具

| 来源 | 加载方式 | 缓存策略 |
|------|----------|----------|
| `extensions_config.json → mcpServers` | 懒加载，首次使用时初始化 | mtime 比较失效 |
| 传输协议：stdio / SSE / HTTP | `langchain-mcp-adapters MultiServerMCPClient` | 运行时 API 更新触发重加载 |
| OAuth 支持 | `client_credentials` / `refresh_token` | 自动令牌刷新 |

### 社区工具

| 工具 | 功能 | 来源模块 |
|------|------|----------|
| `web_search` | Tavily 网络搜索 | `deerflow.community.tavily` |
| `web_fetch` | Tavily 网页抓取 | `deerflow.community.tavily` |
| `jina_ai_web_fetch` | Jina Reader 网页提取 | `deerflow.community.jina_ai` |
| `firecrawl_scrape` | Firecrawl 网页抓取 | `deerflow.community.firecrawl` |
| `image_search` | DuckDuckGo 图像搜索 | `deerflow.community.image_search` |
| `invoke_acp_agent` | 外部 ACP 兼容代理调用 | `deerflow.tools.builtins` |

## 中间件能力

### 始终启用

| 中间件 | 功能 | 生命周期钩子 |
|--------|------|-------------|
| `ThreadDataMiddleware` | 创建线程目录 (workspace/uploads/outputs) | `before_agent` |
| `SandboxMiddleware` | 获取/配置沙箱执行环境 | `before_agent` |
| `DanglingToolCallMiddleware` | 修补悬挂的工具调用（中断恢复） | `wrap_model_call` |
| `LLMErrorHandlingMiddleware` | 瞬态错误重试 + 熔断器 | `wrap_model_call` |
| `SandboxAuditMiddleware` | Bash 命令安全审计 | `wrap_tool_call` |
| `ToolErrorHandlingMiddleware` | 工具异常转 ToolMessage | `wrap_tool_call` |
| `DynamicContextMiddleware` | 记忆/日期动态注入 | `before_agent` |
| `TitleMiddleware` | 自动标题生成 | `after_model` |
| `MemoryMiddleware` | 记忆更新排队 | `after_agent` |
| `ClarificationMiddleware` | 澄清请求拦截 → 中断执行 | `wrap_tool_call` |

### 特性开关控制

| 中间件 | 功能 | 启用条件 | 特性字段 |
|--------|------|----------|----------|
| `SummarizationMiddleware` | 对话摘要压缩 | `summarization.enabled` + 自定义实例 | `RuntimeFeatures.summarization` |
| `TodoMiddleware` | 任务追踪 + 防提前退出 | `is_plan_mode=True` | `plan_mode` 参数 |
| `TokenUsageMiddleware` | Token 用量 + 步骤归属 | `token_usage.enabled` | — |
| `ViewImageMiddleware` | 图像 base64 注入 | 模型 `supports_vision: true` | `RuntimeFeatures.vision` |
| `SubagentLimitMiddleware` | 截断多余并行 task 调用 | `subagent_enabled=True` | `RuntimeFeatures.subagent` |
| `LoopDetectionMiddleware` | 双层循环检测 + 强制停止 | `loop_detection.enabled` | `RuntimeFeatures.loop_detection` |
| `DeferredToolFilterMiddleware` | 隐藏延迟工具 schema | `tool_search.enabled` | — |
| `GuardrailMiddleware` | 工具调用前置授权 | `guardrails.enabled` + provider | `RuntimeFeatures.guardrail` |
| `UploadsMiddleware` | 上传文件注入上下文 | `RuntimeFeatures.sandbox` | `RuntimeFeatures.sandbox` |

## 技能能力

| 能力 | 来源 | 加载机制 |
|------|------|----------|
| 技能发现 | `deerflow.skills.storage` | 递归扫描 `skills/{public,custom}/` 下的 `SKILL.md` |
| 技能注入 | `prompt.py → get_skills_prompt_section()` | 列入系统提示词 `<available_skills>` 块 |
| 渐进式加载 | 系统提示词中的说明 | Agent 按 `read_file` 路径按需读取技能文件 |
| 技能过滤 | `filter_tools_by_skill_allowed_tools()` | 技能 `allowed-tools` 白名单 |
| 技能自演化 | `skill_evolution.enabled` | 系统提示词中注入 Skill Self-Evolution 段落 |
| 运行时安装 | `POST /api/skills/install` | 解压 .skill ZIP 到 custom/ 目录 |

## 记忆能力

| 能力 | 来源 | 说明 |
|------|------|------|
| 事实提取 | `deerflow.agents.memory.updater` | LLM 从对话中提取事实（id, content, category, confidence） |
| 上下文维护 | `storage.py` | `workContext`, `personalContext`, `topOfMind` 摘要 |
| 防抖队列 | `queue.py` | 30s 防抖，per-thread 去重 |
| 信号检测 | `message_processing.py` | 检测纠正（correction）和强化（reinforcement）信号 |
| 注入 | `DynamicContextMiddleware` | Top 15 事实 + 上下文，注入到 `<system-reminder>` |
| Per-User 隔离 | `storage.py` | `users/{user_id}/memory.json` |
| Per-Agent 隔离 | `storage.py` | `users/{user_id}/agents/{agent_name}/memory.json` |
| 摘要前刷入 | `summarization_hook.py` | 摘要压缩前先刷入排队中的记忆 |

## 子代理能力

| 能力 | 来源 | 说明 |
|------|------|------|
| 并行执行 | `deerflow.subagents.executor` | 双线程池：调度池 (3) + 执行池 (3) |
| 并发限制 | `SubagentLimitMiddleware` | 默认最大 3，硬范围 [2, 4] |
| 超时控制 | `executor.py` | 15 分钟超时 |
| 内置类型 | `subagents/builtins/` | `general-purpose`（全工具）+ `bash`（命令专家） |
| 自定义类型 | `subagents/registry.py` | 从配置注册自定义子代理 |
| SSE 事件 | `executor.py` | `task_started` / `task_running` / `task_completed` / `task_failed` / `task_timed_out` |
