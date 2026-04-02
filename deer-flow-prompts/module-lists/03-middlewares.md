# 03-Middlewares 模块文件清单

## 模块概述

**路径**：`backend/packages/harness/deerflow/agents/middlewares/`

**核心作用**：实现 Agent 执行流程中的横切关注点（安全、监控、数据注入、状态管理）

**设计理念**：责任链模式 + 中间件钩子（before_model, after_model, wrap_tool_call 等）

## 文件清单

### 1. __init__.py
- **路径**：`middlewares/__init__.py`
- **职责**：模块入口（空文件）

### 2. thread_data_middleware.py
- **路径**：`middlewares/thread_data_middleware.py`
- **核心类**：
  - `ThreadDataMiddlewareState` - 状态模式定义
  - `ThreadDataMiddleware` - 线程数据目录管理中间件
    - `__init__(base_dir, lazy_init)` - 初始化路径配置
    - `_get_thread_paths(thread_id)` - 获取线程目录路径
    - `_create_thread_directories(thread_id)` - 创建线程目录
    - `before_agent(state, runtime)` - 在 agent 执行前创建目录结构
- **职责**：为每个线程创建隔离的目录结构（workspace, uploads, outputs）
- **目录结构**：
  - `{base_dir}/threads/{thread_id}/user-data/workspace`
  - `{base_dir}/threads/{thread_id}/user-data/uploads`
  - `{base_dir}/threads/{thread_id}/user-data/outputs`

### 3. uploads_middleware.py
- **路径**：`middlewares/uploads_middleware.py`
- **核心类**：
  - `UploadsMiddlewareState` - 状态模式定义
  - `UploadsMiddleware` - 文件上传信息注入中间件
    - `_create_files_message(new_files, historical_files)` - 创建文件列表消息
    - `_files_from_kwargs(message, uploads_dir)` - 从消息中提取文件信息
    - `before_agent(state, runtime)` - 在 agent 执行前注入上传文件信息
- **职责**：
  - 读取消息中的 `additional_kwargs.files`（前端上传后设置）
  - 生成 `<uploaded_files>` 格式化消息块
  - 区分新上传文件和历史文件
  - 将文件信息注入到最后一条 human message 中

### 4. sandbox_audit_middleware.py
- **路径**：`middlewares/sandbox_audit_middleware.py`
- **核心类**：
  - `SandboxAuditMiddleware` - Bash 命令安全审计中间件
    - `_classify_command(command)` - 命令分类（block/warn/pass）
    - `_write_audit(thread_id, command, verdict)` - 写入审计日志
    - `_build_block_message(request, reason)` - 构建阻止消息
    - `_append_warn_to_result(result, command)` - 追加警告到结果
    - `wrap_tool_call(request, handler)` - 拦截 bash 工具调用
- **职责**：
  - 高风险命令阻止（`rm -rf /`, `curl|sh`, `dd` 等）
  - 中风险命令警告（`pip install`, `chmod 777` 等）
  - 记录所有 bash 调用到审计日志
- **风险模式**：
  - 高风险：`rm -rf /*`, `curl url | sh`, `dd if=`, `mkfs`
  - 中风险：`pip install`, `chmod 777`, `apt-get install`

### 5. dangling_tool_call_middleware.py
- **路径**：`middlewares/dangling_tool_call_middleware.py`
- **核心类**：
  - `DanglingToolCallMiddleware` - 悬空工具调用修复中间件
    - `_build_patched_messages(messages)` - 构建修复后的消息列表
    - `wrap_model_call(request, handler)` - 在模型调用前修复消息历史
- **职责**：
  - 检测 AIMessage 中的 tool_calls 缺少对应 ToolMessage 的情况
  - 为悬空工具调用注入占位符 ToolMessage
  - 防止因消息格式不完整导致的 LLM 错误
- **触发场景**：用户中断、请求取消等导致工具调用未完成

### 6. loop_detection_middleware.py
- **路径**：`middlewares/loop_detection_middleware.py`
- **核心类**：
  - `LoopDetectionMiddleware` - 循环检测与中断中间件
    - `_hash_tool_calls(tool_calls)` - 工具调用哈希计算
    - `_get_thread_id(runtime)` - 提取线程 ID
    - `_evict_if_needed()` - LRU 淘汰旧线程跟踪
    - `_track_and_check(state, runtime)` - 跟踪并检测循环
    - `after_model(state, runtime)` - 模型调用后检查
    - `reset(thread_id)` - 重置跟踪状态
- **职责**：
  - 检测重复的工具调用模式
  - 达到警告阈值时注入警告消息
  - 达到硬限制时强制停止（移除 tool_calls）
- **默认参数**：
  - `warn_threshold = 3` - 3 次重复后警告
  - `hard_limit = 5` - 5 次重复后强制停止
  - `window_size = 20` - 滑动窗口大小

### 7. subagent_limit_middleware.py
- **路径**：`middlewares/subagent_limit_middleware.py`
- **核心类**：
  - `SubagentLimitMiddleware` - 子代理并发限制中间件
    - `_truncate_task_calls(state)` - 截断超出的 task 工具调用
    - `after_model(state, runtime)` - 模型调用后检查并截断
- **职责**：
  - 强制限制单次模型响应中的并发子代理调用数量
  - 截断超出限制的 task 工具调用
  - 范围：[2, 4]，默认 3

### 8. title_middleware.py
- **路径**：`middlewares/title_middleware.py`
- **核心类**：
  - `TitleMiddlewareState` - 状态模式定义
  - `TitleMiddleware` - 自动标题生成中间件
    - `_normalize_content(content)` - 标准化消息内容
    - `_should_generate_title(state)` - 判断是否应该生成标题
    - `_build_title_prompt(state)` - 构建标题生成提示词
    - `_parse_title(content)` - 解析模型输出为标题
    - `_fallback_title(user_msg)` - 降级为用户消息摘要
    - `_generate_title_result(state)` - 同步生成标题
    - `_agenerate_title_result(state)` - 异步生成标题
    - `after_model(state, runtime)` - 模型调用后生成标题
- **职责**：
  - 在首次用户-助手交互后自动生成线程标题
  - 使用独立 LLM 调用生成简洁标题
  - 支持同步和异步生成
  - 失败时降级为用户消息摘要

### 9. memory_middleware.py
- **路径**：`middlewares/memory_middleware.py`
- **核心类**：
  - `MemoryMiddlewareState` - 状态模式定义
  - `_filter_messages_for_memory(messages)` - 过滤消息用于记忆更新
  - `MemoryMiddleware` - 记忆更新队列中间件
    - `__init__(agent_name)` - 初始化（支持按 agent 存储记忆）
    - `after_agent(state, runtime)` - agent 执行后将对话加入更新队列
- **职责**：
  - 过滤消息：保留用户输入和最终 AI 响应
  - 移除工具消息和中间步骤
  - 移除 `<uploaded_files>` 临时块
  - 将过滤后的对话加入防抖更新队列
- **过滤规则**：
  - 保留：Human messages, 无 tool_calls 的 AI messages
  - 跳过：Tool messages, 带 tool_calls 的 AI messages

### 10. view_image_middleware.py
- **路径**：`middlewares/view_image_middleware.py`
- **核心类**：
  - `ViewImageMiddlewareState` - 状态模式定义
  - `ViewImageMiddleware` - 图像数据注入中间件
    - `_get_last_assistant_message(messages)` - 获取最后一条助手消息
    - `_has_view_image_tool(message)` - 检查是否包含 view_image 工具调用
    - `_all_tools_completed(messages, assistant_msg)` - 检查所有工具是否完成
    - `_create_image_details_message(state)` - 创建图像详情消息
    - `_should_inject_image_message(state)` - 判断是否应注入图像消息
    - `_inject_image_message(state)` - 注入图像消息
    - `before_model(state, runtime)` - 模型调用前注入图像数据
- **职责**：
  - 检测 view_image 工具调用完成
  - 将图像的 base64 数据注入到对话中
  - 使 LLM 能够"看到"和分析图像
  - 支持多图场景

### 11. clarification_middleware.py
- **路径**：`middlewares/clarification_middleware.py`
- **核心类**：
  - `ClarificationMiddlewareState` - 状态模式定义
  - `ClarificationMiddleware` - 澄清请求拦截中间件
    - `_is_chinese(text)` - 检测是否包含中文字符
    - `_format_clarification_message(args)` - 格式化澄清消息
    - `_handle_clarification(request)` - 处理澄清请求
    - `wrap_tool_call(request, handler)` - 拦截 ask_clarification 工具调用
- **职责**：
  - 拦截 `ask_clarification` 工具调用
  - 中断执行并向用户展示澄清问题
  - 等待用户响应后继续
  - 支持多种澄清类型（missing_info, ambiguous_requirement, approach_choice 等）

### 12. todo_middleware.py
- **路径**：`middlewares/todo_middleware.py`
- **核心类**：
  - `TodoMiddleware` - Todo 列表中间件（扩展 TodoListMiddleware）
    - `before_model(state, runtime)` - 模型调用前注入 todo 提醒
- **职责**：
  - 扩展 LangChain 的 TodoListMiddleware
  - 检测 `write_todos` 工具调用是否因上下文截断而不可见
  - 注入 todo 列表提醒消息
  - 防止模型丢失任务跟踪

### 13. deferred_tool_filter_middleware.py
- **路径**：`middlewares/deferred_tool_filter_middleware.py`
- **核心类**：
  - `DeferredToolFilterMiddleware` - 延迟工具过滤中间件
    - `_filter_tools(request)` - 过滤延迟工具 schema
    - `wrap_model_call(request, handler)` - 模型调用前过滤工具
- **职责**：
  - 当 tool_search 启用时，从模型绑定中移除延迟工具 schema
  - ToolNode 仍持有所有工具（包括延迟工具）用于执行路由
  - LLM 只看到活动工具 schema，延迟工具通过 tool_search 运行时发现
  - 节省上下文 token

### 14. token_usage_middleware.py
- **路径**：`middlewares/token_usage_middleware.py`
- **核心类**：
  - `TokenUsageMiddleware` - Token 使用日志中间件
    - `_log_usage(state)` - 记录 token 使用情况
    - `after_model(state, runtime)` - 模型调用后记录日志
- **职责**：
  - 从模型响应的 `usage_metadata` 提取 token 使用信息
  - 记录 input_tokens, output_tokens, total_tokens
  - 用于成本追踪和分析

### 15. tool_error_handling_middleware.py
- **路径**：`middlewares/tool_error_handling_middleware.py`
- **核心类**：
  - `ToolErrorHandlingMiddleware` - 工具错误处理中间件
    - `_build_error_message(request, exc)` - 构建错误 ToolMessage
    - `wrap_tool_call(request, handler)` - 包装工具调用并捕获异常
  - `_build_runtime_middlewares(...)` - 构建运行时中间件链
  - `build_lead_runtime_middlewares(...)` - 构建 lead agent 中间件
  - `build_subagent_runtime_middlewares(...)` - 构建 subagent 中间件
- **职责**：
  - 捕获工具执行异常并转换为错误 ToolMessage
  - 保留 GraphBubbleUp 信号（interrupt/pause/resume）
  - 提供中间件构建工厂函数
  - 集成 GuardrailMiddleware（如果配置启用）

## 中间件执行顺序

### Lead Agent 中间件链

1. **ThreadDataMiddleware** - 创建线程目录
2. **UploadsMiddleware** - 注入上传文件信息
3. **SandboxMiddleware** - 获取沙箱实例
4. **DanglingToolCallMiddleware** - 修复悬空工具调用
5. **GuardrailMiddleware**（可选）- 预授权工具调用
6. **SandboxAuditMiddleware** - Bash 命令安全审计
7. **ToolErrorHandlingMiddleware** - 工具错误处理
8. **SummarizationMiddleware**（可选）- 上下文摘要
9. **TodoListMiddleware**（可选）- 任务跟踪
10. **TitleMiddleware** - 标题生成
11. **MemoryMiddleware** - 记忆更新
12. **ViewImageMiddleware** - 图像数据注入
13. **SubagentLimitMiddleware**（可选）- 子代理并发限制
14. **ClarificationMiddleware** - 澄清请求拦截（必须最后）

### Subagent 中间件链

1. **ThreadDataMiddleware** - 创建线程目录
2. **SandboxMiddleware** - 获取沙箱实例
3. **SandboxAuditMiddleware** - Bash 命令安全审计
4. **ToolErrorHandlingMiddleware** - 工具错误处理

## 核心设计模式

### 1. 责任链模式

- 中间件按顺序执行
- 每个 middleware 可以：
  - 修改 state
  - 中断执行（return Command(goto=END)）
  - 继续传递（return None 或修改后的 state）

### 2. 钩子点

LangChain AgentMiddleware 提供的钩子：
- `before_agent / after_agent` - Agent 执行前后
- `before_model / after_model` - 模型调用前后
- `wrap_model_call / awrap_model_call` - 包装模型调用（修改请求/响应）
- `wrap_tool_call / awrap_tool_call` - 包装工具调用（异常处理）

### 3. 状态模式

每个 middleware 定义自己的 `state_schema`：
- 继承自 `AgentState`
- 使用 `NotRequired` 标记可选字段
- 兼容 `ThreadState` 完整模式

### 4. 同步/异步双实现

所有 middleware 同时实现同步和异步方法：
- `before_model` / `abefore_model`
- `after_model` / `aafter_model`
- `wrap_model_call` / `awrap_model_call`
- `wrap_tool_call` / `awrap_tool_call`

## 关键依赖

- `langchain.agents.middleware.AgentMiddleware` - 中间件基类
- `langchain_core.messages` - 消息类型（AIMessage, HumanMessage, ToolMessage）
- `langgraph.runtime.Runtime` - 运行时上下文
- `langgraph.types.Command` - 控制流命令
- `langgraph.prebuilt.tool_node.ToolCallRequest` - 工具调用请求
- `deerflow.config.*` - 配置系统
- `deerflow.agents.thread_state` - 线程状态定义
- `deerflow.agents.memory.queue` - 记忆更新队列

## 相关模块

- **依赖**：
  - `deerflow.config` - 配置系统
  - `deerflow.agents.thread_state` - ThreadState 定义
  - `deerflow.agents.memory.queue` - 记忆队列
  - `deerflow.sandbox.middleware` - 沙箱中间件
  - `deerflow.guardrails.middleware` - 安全护栏中间件
  - `deerflow.tools.builtins.tool_search` - 延迟工具注册表
- **被依赖**：
  - `deerflow.agents.lead_agent.agent` - Lead agent 组装
  - `deerflow.subagents.executor` - Subagent 组装

## 配置相关

### TitleMiddleware 配置
- `config.yaml` → `title` 段
- `enabled` - 是否启用
- `max_words` - 标题最大词数
- `max_chars` - 标题最大字符数
- `model_name` - 用于生成标题的模型

### MemoryMiddleware 配置
- `config.yaml` → `memory` 段
- `enabled` - 是否启用
- `storage_path` - 记忆文件路径
- `debounce_seconds` - 防抖时间

### GuardrailMiddleware 配置
- `config.yaml` → `guardrails` 段
- `enabled` - 是否启用
- `provider` - 护栏提供者类路径
- `fail_closed` - 失败时是否阻止
- `passport` - 护栏护照配置
