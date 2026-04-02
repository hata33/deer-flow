# Sandbox 模块文件清单

## 模块概述

Sandbox 模块实现沙箱执行环境，提供隔离的命令执行和文件操作能力。支持本地文件系统沙箱和远程容器沙箱（通过社区扩展）。

## 文件清单

### 1. `/data/deer-flow-main/backend/packages/harness/deerflow/sandbox/__init__.py`

**核心导出**:
- `Sandbox` - 沙箱抽象基类
- `SandboxProvider` - 沙箱提供者协议
- `get_sandbox_provider()` - 获取沙箱提供者单例

**职责**: Sandbox 模块的统一导出入口

---

### 2. `/data/deer-flow-main/backend/packages/harness/deerflow/sandbox/sandbox.py`

**核心类/函数**:
- `Sandbox` - 沙箱抽象基类
  - `id` - 沙箱标识符
  - `execute_command(command)` - 执行 bash 命令
  - `read_file(path)` - 读取文件内容
  - `list_dir(path, max_depth)` - 列出目录内容
  - `write_file(path, content, append)` - 写入文件
  - `update_file(path, content)` - 更新二进制文件

**职责**: 沙箱接口定义

---

### 3. `/data/deer-flow-main/backend/packages/harness/deerflow/sandbox/sandbox_provider.py`

**核心类/函数**:
- `SandboxProvider` - 沙箱提供者抽象协议
  - `acquire(thread_id)` - 获取沙箱环境
  - `get(sandbox_id)` - 获取沙箱实例
  - `release(sandbox_id)` - 释放沙箱
- `get_sandbox_provider()` - 获取沙箱提供者单例
- `reset_sandbox_provider()` - 重置沙箱提供者
- `shutdown_sandbox_provider()` - 关闭并清理沙箱提供者
- `set_sandbox_provider(provider)` - 设置自定义沙箱提供者

**职责**: 沙箱提供者协议和单例管理

---

### 4. `/data/deer-flow-main/backend/packages/harness/deerflow/sandbox/middleware.py`

**核心类/函数**:
- `SandboxMiddlewareState` - 中间件状态模式
- `SandboxMiddleware` - 沙箱中间件
  - `lazy_init` - 延迟初始化标志
  - `_acquire_sandbox(thread_id)` - 获取沙箱
  - `before_agent()` - 代理前钩子（可选初始化）
  - `after_agent()` - 代理后钩子（释放沙箱）

**职责**: 沙箱生命周期管理

---

### 5. `/data/deer-flow-main/backend/packages/harness/deerflow/sandbox/security.py`

**核心类/函数**:
- `LOCAL_HOST_BASH_DISABLED_MESSAGE` - 主机 bash 禁用消息
- `LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE` - Bash 子代理禁用消息
- `uses_local_sandbox_provider(config)` - 检查是否使用本地沙箱
- `is_host_bash_allowed(config)` - 检查主机 bash 是否允许

**职责**: 沙箱安全门控

---

### 6. `/data/deer-flow-main/backend/packages/harness/deerflow/sandbox/tools.py`

**核心类/函数**:
- `bash_tool` - Bash 命令执行工具
- `ls_tool` - 目录列表工具
- `read_file_tool` - 文件读取工具（支持行范围）
- `write_file_tool` - 文件写入工具
- `str_replace_tool` - 字符串替换工具
- `ensure_sandbox_initialized(runtime)` - 确保沙箱已初始化
- `ensure_thread_directories_exist(runtime)` - 确保线程目录存在
- `replace_virtual_path(path, thread_data)` - 虚拟路径替换
- `mask_local_paths_in_output(output, thread_data)` - 输出路径掩码
- `validate_local_tool_path(path, thread_data, read_only)` - 本地工具路径验证
- `validate_local_bash_command_paths(command, thread_data)` - Bash 命令路径验证
- `is_local_sandbox(runtime)` - 检查是否本地沙箱

**职责**: 沙箱工具实现和虚拟路径系统

---

### 7. `/data/deer-flow-main/backend/packages/harness/deerflow/sandbox/exceptions.py`

**核心类/函数**:
- `SandboxError` - 沙箱错误基类
- `SandboxNotFoundError` - 沙箱未找到错误
- `SandboxRuntimeError` - 沙箱运行时错误
- `SandboxCommandError` - 命令执行错误
- `SandboxFileError` - 文件操作错误
- `SandboxPermissionError` - 权限错误
- `SandboxFileNotFoundError` - 文件未找到错误

**职责**: 沙箱异常定义

---

### 8. `/data/deer-flow-main/backend/packages/harness/deerflow/sandbox/local/__init__.py`

**核心导出**:
- `LocalSandboxProvider` - 本地沙箱提供者

**职责**: 本地沙箱模块导出

---

### 9. `/data/deer-flow-main/backend/packages/harness/deerflow/sandbox/local/local_sandbox.py`

**核心类/函数**:
- `LocalSandbox` - 本地文件系统沙箱实现
  - `path_mappings` - 路径映射字典
  - `_resolve_path(path)` - 解析容器路径到本地路径
  - `_reverse_resolve_path(path)` - 反向解析本地路径到容器路径
  - `_resolve_paths_in_command(command)` - 解析命令中的路径
  - `_reverse_resolve_paths_in_output(output)` - 反向解析输出中的路径
  - `execute_command(command)` - 执行命令
  - `list_dir(path, max_depth)` - 列出目录
  - `read_file(path)` - 读取文件
  - `write_file(path, content, append)` - 写入文件
  - `update_file(path, content)` - 更新二进制文件

**职责**: 本地沙箱实现

---

### 10. `/data/deer-flow-main/backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py`

**核心类/函数**:
- `LocalSandboxProvider` - 本地沙箱提供者
  - `_path_mappings` - 路径映射配置
  - `_setup_path_mappings()` - 设置路径映射
  - `acquire(thread_id)` - 获取本地沙箱（单例）
  - `get(sandbox_id)` - 获取本地沙箱实例
  - `release(sandbox_id)` - 释放沙箱（无操作）

**职责**: 本地沙箱提供者实现（单例模式）

---

### 11. `/data/deer-flow-main/backend/packages/harness/deerflow/sandbox/local/list_dir.py`

**核心类/函数**:
- `IGNORE_PATTERNS` - 忽略模式列表（版本控制、依赖、构建输出等）
- `_should_ignore(name)` - 检查是否应忽略
- `list_dir(path, max_depth)` - 递归列出目录内容（带忽略过滤）

**职责**: 目录列表工具（带忽略过滤）

---

## 虚拟路径系统

**虚拟路径映射**:
- `/mnt/user-data/workspace` → `{base_dir}/threads/{thread_id}/user-data/workspace`
- `/mnt/user-data/uploads` → `{base_dir}/threads/{thread_id}/user-data/uploads`
- `/mnt/user-data/outputs` → `{base_dir}/threads/{thread_id}/user-data/outputs`
- `/mnt/skills` → 技能目录
- `/mnt/acp-workspace` → ACP 工作空间

**安全特性**:
- 路径遍历检测（拒绝 `..` 片段）
- 本地沙箱 bash 执行门控
- 技能路径只读保护
- 输出路径掩码（隐藏主机路径）
