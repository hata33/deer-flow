# 08-工具系统模块文件清单

## 模块概述

**路径**：`backend/packages/harness/deerflow/tools/`

**核心作用**：提供工具发现、加载和管理机制，支持内置工具、MCP 工具、社区工具和子代理工具

**设计理念**：动态工具加载 + 延迟注册 + 虚拟路径隔离

## 文件清单

### 1. __init__.py
- **路径**：`tools/__init__.py`
- **核心导出**：
  - `get_available_tools` - 获取所有可用工具
- **职责**：模块入口，导出核心工具函数

### 2. tools.py
- **路径**：`tools/tools.py`
- **核心函数**：
  - `get_available_tools()` - 获取所有可用工具
    - `groups` - 按工具组过滤
    - `include_mcp` - 是否包含 MCP 工具
    - `model_name` - 模型名称（用于判断是否支持视觉）
    - `subagent_enabled` - 是否启用子代理工具
  - `_is_host_bash_tool()` - 判断是否为主机 bash 工具
- **常量**：
  - `BUILTIN_TOOLS` - 内置工具列表
  - `SUBAGENT_TOOLS` - 子代理工具列表
- **职责**：工具加载和编排

**工具加载流程**：
1. 从配置加载自定义工具（通过 `resolve_variable` 解析）
2. 过滤主机 bash 工具（如果沙箱不允许）
3. 添加内置工具（present_file, ask_clarification）
4. 根据模型能力添加 view_image 工具
5. 加载 MCP 工具（如果启用）
6. 加载 ACP 代理工具（如果配置）
7. 如果启用子代理，添加 task 工具

### 3. builtins/__init__.py
- **路径**：`tools/builtins/__init__.py`
- **核心导出**：
  - `ask_clarification_tool` - 请求澄清工具
  - `present_file_tool` - 展示文件工具
  - `view_image_tool` - 查看图片工具
  - `task_tool` - 任务委托工具
  - `setup_agent` - 设置代理工具
- **职责**：内置工具模块入口

### 4. builtins/clarification_tool.py
- **路径**：`tools/builtins/clarification_tool.py`
- **核心函数**：
  - `ask_clarification_tool()` - 请求用户澄清
    - `question` - 澄清问题
    - `clarification_type` - 澄清类型
    - `context` - 可选上下文
    - `options` - 可选选项列表
- **职责**：中断执行并请求用户输入

**澄清类型**：
- `missing_info` - 缺少信息
- `ambiguous_requirement` - 需求模糊
- `approach_choice` - 方案选择
- `risk_confirmation` - 风险确认
- `suggestion` - 建议确认

**工作机制**：实际逻辑由 `ClarificationMiddleware` 拦截处理，工具本身是占位实现

### 5. builtins/present_file_tool.py
- **路径**：`tools/builtins/present_file_tool.py`
- **核心函数**：
  - `present_file_tool()` - 展示文件给用户
    - `filepaths` - 文件路径列表
  - `_normalize_presented_filepath()` - 规范化文件路径
- **职责**：将输出目录的文件展示给用户

**路径限制**：只能展示 `/mnt/user-data/outputs/*` 下的文件

**虚拟路径系统**：
- 支持虚拟路径：`/mnt/user-data/outputs/report.md`
- 支持物理路径：`/app/backend/.deer-flow/threads/<thread>/user-data/outputs/report.md`
- 自动规范化为虚拟路径格式

### 6. builtins/view_image_tool.py
- **路径**：`tools/builtins/view_image_tool.py`
- **核心函数**：
  - `view_image_tool()` - 读取图片文件
    - `image_path` - 图片文件路径
- **职责**：读取图片并转换为 base64 供模型理解

**支持的格式**：jpg, jpeg, png, webp

**工作流程**：
1. 替换虚拟路径为实际路径
2. 验证路径存在且为文件
3. 验证文件扩展名
4. 读取文件并转换为 base64
5. 更新 `viewed_images` 状态

### 7. builtins/task_tool.py
- **路径**：`tools/builtins/task_tool.py`
- **核心函数**：
  - `task_tool()` - 委托任务给子代理
    - `description` - 任务描述（3-5 词）
    - `prompt` - 详细任务提示
    - `subagent_type` - 子代理类型
    - `max_turns` - 最大轮数（可选）
- **职责**：任务委托和子代理管理

**子代理类型**：
- `general-purpose` - 通用代理（复杂多步骤任务）
- `bash` - 命令执行专家（需要主机 bash 权限）

**工作流程**：
1. 获取子代理配置
2. 构建配置覆盖（系统提示、最大轮数）
3. 提取父上下文（沙箱、线程、模型）
4. 创建执行器并启动后台执行
5. 轮询任务状态并发送事件
6. 返回最终结果

**事件流**：
- `task_started` - 任务开始
- `task_running` - 任务运行中（包含 AI 消息）
- `task_completed` - 任务完成
- `task_failed` - 任务失败
- `task_timed_out` - 任务超时

**轮询机制**：
- 每 5 秒轮询一次
- 超时时间：执行超时 + 60 秒缓冲
- 实时发送 AI 消息更新

### 8. builtins/tool_search.py
- **路径**：`tools/builtins/tool_search.py`
- **核心类**：
  - `DeferredToolEntry` - 延迟工具条目
    - `name` - 工具名称
    - `description` - 工具描述
    - `tool` - 完整工具对象
  - `DeferredToolRegistry` - 延迟工具注册表
    - `register()` - 注册工具
    - `promote()` - 提升工具为活跃状态
    - `search()` - 搜索工具
- **核心函数**：
  - `tool_search()` - 搜索延迟工具
  - `get_deferred_registry()` - 获取当前注册表
  - `set_deferred_registry()` - 设置注册表
  - `reset_deferred_registry()` - 重置注册表
- **职责**：延迟工具发现和搜索

**搜索模式**：
1. `select:name1,name2` - 精确名称匹配
2. `+keyword rest` - 名称必须包含关键词，按其余词排序
3. `keyword query` - 正则表达式搜索名称和描述

**ContextVar 机制**：使用 `contextvars` 确保每个请求有独立的注册表，避免并发冲突

**工作流程**：
1. MCP 工具注册到延迟注册表
2. 工具名称出现在系统提示的 `<available-deferred-tools>` 中
3. LLM 调用 `tool_search` 获取完整 schema
4. 匹配的工具被提升为活跃状态
5. LLM 可以直接调用这些工具

### 9. builtins/invoke_acp_agent_tool.py
- **路径**：`tools/builtins/invoke_acp_agent_tool.py`
- **核心类**：
  - `_InvokeACPAgentInput` - ACP 代理输入模型
  - `_CollectingClient` - ACP 客户端（收集文本）
- **核心函数**：
  - `build_invoke_acp_agent_tool()` - 构建 ACP 代理工具
  - `_get_work_dir()` - 获取工作目录
  - `_build_mcp_servers()` - 构建 MCP 服务器配置
  - `_build_permission_response()` - 构建权限响应
  - `_format_invocation_error()` - 格式化调用错误
- **职责**：调用外部 ACP 兼容代理

**工作空间隔离**：
- 每个线程独立工作空间：`{base_dir}/threads/{thread_id}/acp-workspace/`
- 虚拟路径：`/mnt/acp-workspace/`（只读）
- Docker 沙箱模式：卷挂载到容器

**权限处理**：
- `auto_approve: true` - 自动批准权限请求
- `auto_approve: false` - 拒绝权限请求（默认）

**错误处理**：
- 检测可执行文件缺失
- 提供可操作的错误消息
- 支持 `codex-acp` 适配器提示

### 10. builtins/setup_agent_tool.py
- **路径**：`tools/builtins/setup_agent_tool.py`
- **核心函数**：
  - `setup_agent()` - 设置自定义代理
    - `soul` - SOUL.md 内容
    - `description` - 代理描述
- **职责**：创建自定义代理配置

**工作流程**：
1. 获取代理目录（基于 agent_name）
2. 创建 `config.yaml`（如果提供了 agent_name）
3. 写入 `SOUL.md` 文件
4. 返回成功或错误消息

**错误处理**：失败时清理已创建的目录

## 核心设计模式

### 1. 工具分层架构
```
配置工具 (config.yaml)
    ↓
内置工具 (builtins/)
    ↓
MCP 工具 (延迟加载)
    ↓
社区工具 (community/)
    ↓
子代理工具 (subagents/)
```

### 2. 延迟加载模式
- MCP 工具首次使用时加载
- 缓存机制（mtime 失效）
- 延迟注册表（ContextVar 隔离）

### 3. 虚拟路径系统
- 统一的虚拟路径前缀：`/mnt/user-data/`
- 自动路径转换
- 线程隔离

### 4. 事件流模式
- 子代理任务通过 SSE 事件流式更新
- 实时进度反馈
- 支持中断和恢复

### 5. 中间件拦截
- `ClarificationMiddleware` 拦截 `ask_clarification`
- `ViewImageMiddleware` 注入图片数据
- `DeferredToolFilterMiddleware` 过滤延迟工具

## 工具分类

### 内置工具（BUILTIN_TOOLS）
- `present_files` - 展示文件
- `ask_clarification` - 请求澄清

### 条件内置工具
- `view_image` - 查看图片（需要模型支持视觉）
- `task` - 任务委托（需要启用子代理）

### MCP 工具
- 从 `extensions_config.json` 加载
- 支持多种传输方式（stdio, SSE, HTTP）
- 自动缓存和失效

### ACP 工具
- `invoke_acp_agent` - 调用外部 ACP 代理
- 从 `config.yaml` 加载配置
- 支持多个代理实例

## 关键依赖

- `langchain.tools` - 工具基础框架
- `langchain_core.tools` - 核心工具类型
- `langgraph` - 图执行和命令系统
- `pydantic` - 数据验证
- `deerflow.config` - 配置系统
- `deerflow.sandbox` - 沙箱系统
- `deerflow.subagents` - 子代理系统

## 相关模块

- **被依赖**：`agents/lead_agent` - 主代理使用工具
- **依赖**：
  - `config/` - 工具配置
  - `sandbox/` - 沙箱工具
  - `subagents/` - 子代理系统
  - `mcp/` - MCP 工具集成
  - `reflection/` - 动态模块加载

## 配置要点

### config.yaml
```yaml
tools:
  - group: bash
    use: deerflow.sandbox.tools:bash_tool

tool_search:
  enabled: true

subagents:
  enabled: true
```

### extensions_config.json
```json
{
  "mcpServers": {
    "server-name": {
      "enabled": true,
      "type": "stdio",
      "command": "node",
      "args": ["server.js"]
    }
  }
}
```

## 安全考虑

### 主机 Bash 保护
- 默认禁用主机 bash 工具
- 沙箱模式下自动过滤
- `is_host_bash_allowed()` 检查

### 路径隔离
- 虚拟路径系统限制访问范围
- `present_files` 只能展示输出目录
- 线程级别隔离

### 权限管理
- ACP 代理权限请求处理
- 可配置自动批准策略
- 明确的权限拒绝消息
