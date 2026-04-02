# Subagents 模块文件清单

## 模块概述

Subagents 模块实现子代理委托系统，允许主代理将复杂任务委托给专门的子代理在独立上下文中执行。

## 文件清单

### 1. `/data/deer-flow-main/backend/packages/harness/deerflow/subagents/__init__.py`

**核心导出**:
- `SubagentConfig` - 子代理配置模型
- `SubagentExecutor` - 子代理执行器
- `SubagentResult` - 子代理执行结果
- `get_available_subagent_names()` - 获取可用子代理名称
- `get_subagent_config()` - 获取子代理配置
- `list_subagents()` - 列出所有子代理配置

**职责**: 子代理模块的统一导出入口

---

### 2. `/data/deer-flow-main/backend/packages/harness/deerflow/subagents/config.py`

**核心类/函数**:
- `SubagentConfig` - 子代理配置数据类
  - `name` - 唯一标识符
  - `description` - 使用场景描述
  - `system_prompt` - 系统提示词
  - `tools` - 允许的工具列表
  - `disallowed_tools` - 禁止的工具列表
  - `model` - 使用的模型
  - `max_turns` - 最大轮次
  - `timeout_seconds` - 超时时间（默认 900 秒）

**职责**: 子代理配置定义

---

### 3. `/data/deer-flow-main/backend/packages/harness/deerflow/subagents/executor.py`

**核心类/函数**:
- `SubagentStatus` - 执行状态枚举（PENDING/RUNNING/COMPLETED/FAILED/TIMED_OUT）
- `SubagentResult` - 执行结果数据类
  - `task_id` - 任务 ID
  - `trace_id` - 追踪 ID
  - `status` - 当前状态
  - `result` - 最终结果
  - `error` - 错误信息
  - `ai_messages` - AI 消息列表
- `SubagentExecutor` - 子代理执行器
  - `_create_agent()` - 创建代理实例
  - `_build_initial_state()` - 构建初始状态
  - `_aexecute()` - 异步执行任务
  - `execute()` - 同步执行（包装异步）
  - `execute_async()` - 启动后台执行
- `_scheduler_pool` / `_execution_pool` - 双线程池（调度 + 执行）
- `MAX_CONCURRENT_SUBAGENTS` - 最大并发数（3）
- `get_background_task_result()` - 获取后台任务结果
- `list_background_tasks()` - 列出所有后台任务
- `cleanup_background_task()` - 清理已完成的任务

**职责**: 子代理执行引擎，支持后台执行和超时控制

---

### 4. `/data/deer-flow-main/backend/packages/harness/deerflow/subagents/registry.py`

**核心类/函数**:
- `get_subagent_config()` - 获取子代理配置（应用 config.yaml 覆盖）
- `list_subagents()` - 列出所有子代理配置
- `get_subagent_names()` - 获取所有子代理名称
- `get_available_subagent_names()` - 获取当前沙箱配置下的可用子代理名称

**职责**: 子代理注册表管理，配置覆盖

---

### 5. `/data/deer-flow-main/backend/packages/harness/deerflow/subagents/builtins/__init__.py`

**核心导出**:
- `GENERAL_PURPOSE_CONFIG` - 通用代理配置
- `BASH_AGENT_CONFIG` - Bash 代理配置
- `BUILTIN_SUBAGENTS` - 内置子代理注册表

**职责**: 内置子代理配置

---

### 6. `/data/deer-flow-main/backend/packages/harness/deerflow/subagents/builtins/general_purpose.py`

**核心类/函数**:
- `GENERAL_PURPOSE_CONFIG` - 通用子代理配置
  - 继承所有工具（除 task/ask_clarification/present_files）
  - 用于复杂多步骤任务

**职责**: 通用子代理配置定义

---

### 7. `/data/deer-flow-main/backend/packages/harness/deerflow/subagents/builtins/bash_agent.py`

**核心类/函数**:
- `BASH_AGENT_CONFIG` - Bash 命令执行专家配置
  - 仅沙箱工具（bash, ls, read_file, write_file, str_replace）
  - 专用于终端操作（git, npm, docker 等）

**职责**: Bash 子代理配置定义

---

## 执行流程

1. **任务委托**: 主代理调用 `task` 工具
2. **后台启动**: SubagentExecutor 在后台线程中启动执行
3. **轮询机制**: backend 每 5 秒轮询任务状态
4. **SSE 事件**: 发送 task_started/task_running/task_completed 事件
5. **结果返回**: 完成后返回最终结果
