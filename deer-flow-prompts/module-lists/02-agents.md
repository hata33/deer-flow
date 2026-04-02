# Agents 模块文件清单

## 模块概述

Agents 模块是 DeerFlow 的核心代理系统，基于 LangGraph 实现。包含 Lead Agent（主代理）、中间件链、内存机制、检查点和状态管理。

## 文件清单

### 1. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/__init__.py`

**核心导出**:
- `create_deerflow_agent()` - SDK 级别的代理工厂函数
- `RuntimeFeatures` - 声明式特性标志
- `Next` / `Prev` - 中间件定位装饰器
- `make_lead_agent()` - 应用级 Lead Agent 工厂
- `ThreadState` - 线程状态模式
- `get_checkpointer()` / `make_checkpointer()` - 检查点工厂

**职责**: 代理模块的统一导出入口

---

### 2. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/factory.py`

**核心类/函数**:
- `create_deerflow_agent()` - SDK 级代理工厂（纯参数，无 YAML 配置）
  - `model` - 聊天模型实例
  - `tools` - 工具列表
  - `system_prompt` - 系统提示词
  - `middleware` / `features` / `extra_middleware` - 中间件配置
  - `plan_mode` - TodoList 中间件开关
  - `checkpointer` - 状态持久化后端
- `_assemble_from_features()` - 从 RuntimeFeatures 构建中间件链
- `_insert_extra()` - 通过 @Next/@Prev 插入额外中间件

**职责**: 纯参数代理工厂，位于 LangGraph 原语和配置驱动的 make_lead_agent 之间

---

### 3. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/features.py`

**核心类/函数**:
- `RuntimeFeatures` - 声明式特性标志数据类
  - `sandbox` - 沙箱特性
  - `memory` - 内存特性
  - `summarization` - 摘要特性
  - `subagent` - 子代理特性
  - `vision` - 视觉特性
  - `auto_title` - 自动标题特性
  - `guardrail` - Guardrail 特性
- `Next(anchor)` - 声明中间件应放在 anchor 之后
- `Prev(anchor)` - 声明中间件应放在 anchor 之前

**职责**: 声明式特性标志和中间件定位装饰器（纯数据，无 I/O）

---

### 4. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/thread_state.py`

**核心类/函数**:
- `SandboxState` - 沙箱状态（sandbox_id）
- `ThreadDataState` - 线程数据状态（workspace_path, uploads_path, outputs_path）
- `ViewedImageData` - 已查看图像数据（base64, mime_type）
- `merge_artifacts()` - 工件列表合并和去重 Reducer
- `merge_viewed_images()` - 图像字典合并 Reducer
- `ThreadState` - 扩展 AgentState 的线程状态模式
  - `sandbox` - 沙箱状态
  - `thread_data` - 线程数据
  - `title` - 线程标题
  - `artifacts` - 工件列表（去重）
  - `todos` - Todo 列表
  - `uploaded_files` - 上传文件列表
  - `viewed_images` - 已查看图像映射

**职责**: 定义线程状态模式和自定义 Reducer

---

### 5. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/checkpointer/__init__.py`

**核心导出**:
- `get_checkpointer()` - 获取同步检查点单例
- `reset_checkpointer()` - 重置检查点单例
- `checkpointer_context()` - 同步上下文管理器
- `make_checkpointer()` - 异步上下文管理器

**职责**: 检查点模块的统一导出入口

---

### 6. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/checkpointer/provider.py`

**核心类/函数**:
- `_sync_checkpointer_cm()` - 同步检查点上下文管理器
  - 支持 `memory`、`sqlite`、`postgres` 后端
- `get_checkpointer()` - 返回全局同步检查点单例
- `reset_checkpointer()` - 重置单例，强制重新创建
- `checkpointer_context()` - 同步上下文管理器（一次性）

**职责**: 同步检查点工厂，提供单例和上下文管理器

---

### 7. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/checkpointer/async_provider.py`

**核心类/函数**:
- `_async_checkpointer()` - 异步检查点上下文管理器
  - 支持 `memory`、`sqlite`、`postgres` 后端
- `make_checkpointer()` - 异步上下文管理器（用于 FastAPI lifespan）

**职责**: 异步检查点工厂，用于长期运行的异步服务器

---

### 8. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/lead_agent/__init__.py`

**核心导出**:
- `make_lead_agent()` - Lead Agent 工厂函数

**职责**: Lead Agent 模块的统一导出入口

---

### 9. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/lead_agent/agent.py`

**核心类/函数**:
- `make_lead_agent(config)` - Lead Agent 工厂函数
  - 解析运行时配置（thinking_enabled, model_name, is_plan_mode, subagent_enabled）
  - 创建聊天模型（支持思考模式和推理强度）
  - 构建中间件链（14 个中间件）
  - 生成系统提示词
  - 注册到 langgraph.json
- `_resolve_model_name()` - 安全解析模型名称
- `_create_summarization_middleware()` - 创建摘要中间件
- `_create_todo_list_middleware()` - 创建 TodoList 中间件
- `_build_middlewares()` - 构建完整中间件链

**职责**: Lead Agent 工厂，协调模型、工具、中间件和系统提示词

---

### 10. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/lead_agent/prompt.py`

**核心类/函数**:
- `SYSTEM_PROMPT_TEMPLATE` - 系统提示词模板
- `_build_subagent_section()` - 构建子代理系统提示词
- `apply_prompt_template()` - 应用提示词模板
  - 注入内存上下文
  - 注入技能列表
  - 注入延迟工具列表
  - 注入子代理提示词
  - 注入 ACP 代理提示词
  - 注入 SOUL.md（代理个性）
- `_get_memory_context()` - 获取内存上下文
- `get_skills_prompt_section()` - 生成技能提示词部分
- `get_agent_soul()` - 获取代理 SOUL.md 内容
- `get_deferred_tools_prompt_section()` - 生成延迟工具提示词部分
- `_build_acp_section()` - 构建 ACP 代理提示词部分

**职责**: 生成和管理系统提示词，包括子代理、技能、内存等上下文

---

### 11. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/memory/__init__.py`

**核心导出**:
- `MEMORY_UPDATE_PROMPT` / `FACT_EXTRACTION_PROMPT` - 提示词模板
- `format_memory_for_injection()` / `format_conversation_for_update()` - 格式化函数
- `ConversationContext` / `MemoryUpdateQueue` / `get_memory_queue()` - 队列
- `MemoryStorage` / `FileMemoryStorage` / `get_memory_storage()` - 存储
- `MemoryUpdater` / `clear_memory_data()` / `get_memory_data()` / `reload_memory_data()` / `update_memory_from_conversation()` - 更新器

**职责**: 内存模块的统一导出入口

---

### 12. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/memory/prompt.py`

**核心类/函数**:
- `MEMORY_UPDATE_PROMPT` - 内存更新提示词模板
- `FACT_EXTRACTION_PROMPT` - 事实提取提示词模板
- `format_memory_for_injection()` - 格式化内存数据用于注入（支持 token 计数）
- `format_conversation_for_update()` - 格式化对话用于更新
- `_count_tokens()` - 使用 tiktoken 计算 token 数
- `_coerce_confidence()` - 强制置信度为有效浮点数

**职责**: 内存提示词模板和格式化工具

---

### 13. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/memory/queue.py`

**核心类/函数**:
- `ConversationContext` - 对话上下文数据类
- `MemoryUpdateQueue` - 内存更新队列（带防抖）
  - `add()` - 添加对话到队列
  - `_process_queue()` - 处理队列中的对话
  - `flush()` - 强制立即处理
  - `clear()` - 清空队列
- `get_memory_queue()` - 获取全局队列单例
- `reset_memory_queue()` - 重置全局队列

**职责**: 内存更新队列管理，带防抖机制

---

### 14. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/memory/storage.py`

**核心类/函数**:
- `create_empty_memory()` - 创建空内存结构
- `MemoryStorage` - 内存存储抽象基类
  - `load()` - 加载内存数据
  - `reload()` - 强制重新加载
  - `save()` - 保存内存数据
- `FileMemoryStorage` - 基于文件的内存存储实现
  - 带文件修改时间检查的缓存
  - 原子写入（临时文件 + 重命名）
- `get_memory_storage()` - 获取配置的存储实例

**职责**: 内存存储提供者抽象和文件实现

---

### 15. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/memory/updater.py`

**核心类/函数**:
- `MemoryUpdater` - 使用 LLM 更新内存
  - `update_memory()` - 基于对话更新内存
  - `_apply_updates()` - 应用 LLM 生成的更新
- `get_memory_data()` - 获取当前内存数据
- `reload_memory_data()` - 重新加载内存数据
- `import_memory_data()` - 导入内存数据
- `clear_memory_data()` - 清空内存数据
- `create_memory_fact()` - 创建新事实
- `delete_memory_fact()` - 删除事实
- `update_memory_fact()` - 更新事实
- `_strip_upload_mentions_from_memory()` - 从内存中移除文件上传提及

**职责**: LLM 驱动的内存更新和事实管理

---

## Middlewares 子模块

### 16. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/__init__.py`

**核心导出**: 中间件模块的统一导出入口

**职责**: 中间件模块导出

---

### 17. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/clarification_middleware.py`

**核心类/函数**:
- `ClarificationMiddleware` - 拦截 ask_clarification 工具调用并中断执行
  - `_format_clarification_message()` - 格式化澄清问题
  - `_handle_clarification()` - 处理澄清请求
  - `wrap_tool_call()` / `awrap_tool_call()` - 拦截工具调用

**职责**: 澄清请求拦截和用户交互中断（必须在中间件链最后）

---

### 18. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/dangling_tool_call_middleware.py`

**核心类/函数**:
- `DanglingToolCallMiddleware` - 修复悬空工具调用
  - `_build_patched_messages()` - 构建修复后的消息列表
  - `wrap_model_call()` / `awrap_model_call()` - 在模型调用前修复

**职责**: 检测并修复消息历史中的悬空工具调用（无对应 ToolMessage）

---

### 19. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/deferred_tool_filter_middleware.py`

**核心类/函数**:
- `DeferredToolFilterMiddleware` - 从模型绑定中过滤延迟工具
  - `_filter_tools()` - 移除延迟工具模式
  - `wrap_model_call()` / `awrap_model_call()` - 在模型调用前过滤

**职责**: 当 tool_search 启用时，防止延迟工具模式被发送到 LLM

---

### 20. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py`

**核心类/函数**:
- `LoopDetectionMiddleware` - 检测并中断重复工具调用循环
  - `_hash_tool_calls()` - 工具调用确定性哈希
  - `_track_and_check()` - 跟踪并检查循环
  - `_apply()` - 应用警告或强制停止
  - `reset()` - 清除跟踪状态
- `_DEFAULT_WARN_THRESHOLD` - 警告阈值（3）
- `_DEFAULT_HARD_LIMIT` - 强制停止限制（5）

**职责**: P0 安全防护，防止无限重复工具调用

---

### 21. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py`

**核心类/函数**:
- `MemoryMiddleware` - 队列化对话用于内存更新
  - `after_agent()` / `aafter_agent()` - 代理执行后队列化
- `_filter_messages_for_memory()` - 过滤消息（保留用户输入和最终 AI 响应）

**职责**: 在代理执行后将对话队列化用于异步内存更新

---

### 22. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py`

**核心类/函数**:
- `SandboxAuditMiddleware` - Bash 命令安全审计
  - `_classify_command()` - 命令分类（block/warn/pass）
  - `_write_audit()` - 写入审计日志
  - `_build_block_message()` - 构建阻止消息
  - `_append_warn_to_result()` - 追加警告到结果
  - `wrap_tool_call()` / `awrap_tool_call()` - 审计 bash 工具调用
- `_HIGH_RISK_PATTERNS` - 高风险命令模式（rm -rf /, curl|sh 等）
- `_MEDIUM_RISK_PATTERNS` - 中风险命令模式（chmod 777, pip install 等）

**职责**: Bash 命令安全审计和日志记录

---

### 23. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py`

**核心类/函数**:
- `SubagentLimitMiddleware` - 限制并发子代理工具调用
  - `_truncate_task_calls()` - 截断多余的 task 工具调用
  - `after_model()` / `aafter_model()` - 模型调用后截断
- `MIN_SUBAGENT_LIMIT` / `MAX_SUBAGENT_LIMIT` - 限制范围 [2, 4]

**职责**: 强制执行每响应的最大并发子代理调用数

---

### 24. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/thread_data_middleware.py`

**核心类/函数**:
- `ThreadDataMiddleware` - 创建线程数据目录
  - `_get_thread_paths()` - 获取线程路径
  - `_create_thread_directories()` - 创建目录
  - `before_agent()` - 代理执行前准备路径
  - 支持 lazy_init 延迟初始化

**职责**: 为每个线程创建工作目录（workspace/uploads/outputs）

---

### 25. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/title_middleware.py`

**核心类/函数**:
- `TitleMiddleware` - 自动生成线程标题
  - `_normalize_content()` - 标准化内容（处理列表/字典）
  - `_should_generate_title()` - 检查是否应生成标题
  - `_build_title_prompt()` - 构建标题生成提示词
  - `_parse_title()` - 解析模型输出为干净标题
  - `_fallback_title()` - 回退标题
  - `after_model()` / `aafter_model()` - 模型调用后生成标题

**职责**: 在首次完整交换后自动生成线程标题

---

### 26. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/todo_middleware.py`

**核心类/函数**:
- `TodoMiddleware` - 扩展 TodoListMiddleware，支持上下文丢失检测
  - `before_model()` / `abefore_model()` - 在模型调用前检测并注入提醒
- `_todos_in_messages()` - 检查消息中是否有 write_todos 调用
- `_reminder_in_messages()` - 检查是否已有提醒消息
- `_format_todos()` - 格式化 Todo 列表

**职责**: 检测 write_todos 上下文丢失并注入提醒消息

---

### 27. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/token_usage_middleware.py`

**核心类/函数**:
- `TokenUsageMiddleware` - 记录 LLM Token 使用
  - `_log_usage()` - 记录 usage_metadata
  - `after_model()` / `aafter_model()` - 模型调用后记录

**职责**: 从模型响应中记录 Token 使用统计

---

### 28. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py`

**核心类/函数**:
- `ToolErrorHandlingMiddleware` - 将工具异常转换为错误 ToolMessage
  - `_build_error_message()` - 构建错误消息
  - `wrap_tool_call()` / `awrap_tool_call()` - 包装工具调用
- `build_lead_runtime_middlewares()` - 构建 Lead Agent 运行时中间件
- `build_subagent_runtime_middlewares()` - 构建子代理运行时中间件

**职责**: 工具异常处理和共享运行时中间件构建器

---

### 29. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/uploads_middleware.py`

**核心类/函数**:
- `UploadsMiddleware` - 注入上传文件信息
  - `_create_files_message()` - 创建文件消息
  - `_files_from_kwargs()` - 从 additional_kwargs.files 提取文件信息
  - `before_agent()` - 代理执行前注入文件信息

**职责**: 将上传文件信息注入到代理上下文中

---

### 30. `/data/deer-flow-main/backend/packages/harness/deerflow/agents/middlewares/view_image_middleware.py`

**核心类/函数**:
- `ViewImageMiddleware` - 注入图像详情到对话
  - `_get_last_assistant_message()` - 获取最后助手消息
  - `_has_view_image_tool()` - 检查是否有 view_image 工具调用
  - `_all_tools_completed()` - 检查所有工具是否已完成
  - `_create_image_details_message()` - 创建图像详情消息
  - `_should_inject_image_message()` - 判断是否应注入
  - `_inject_image_message()` - 注入图像消息
  - `before_model()` / `abefore_model()` - 模型调用前注入

**职责**: 在 view_image 工具完成后将图像数据（base64）注入到对话中

---

## 中间件执行顺序

1. **ThreadDataMiddleware** - 创建线程目录
2. **UploadsMiddleware** - 注入上传文件信息
3. **SandboxMiddleware** - 获取沙箱
4. **DanglingToolCallMiddleware** - 修复悬空工具调用
5. **GuardrailMiddleware** - 工具调用前授权（可选）
6. **ToolErrorHandlingMiddleware** - 工具错误处理
7. **SummarizationMiddleware** - 上下文摘要（可选）
8. **TodoMiddleware** - Todo 列表管理（可选）
9. **TitleMiddleware** - 自动标题生成
10. **MemoryMiddleware** - 内存更新队列
11. **ViewImageMiddleware** - 图像数据注入
12. **SubagentLimitMiddleware** - 子代理限制
13. **LoopDetectionMiddleware** - 循环检测
14. **ClarificationMiddleware** - 澄清请求拦截（必须最后）
