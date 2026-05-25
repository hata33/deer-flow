# 内置工具详解

DeerFlow 提供了一系列内置工具，覆盖文件展示、用户澄清、图片查看、任务委派、工具搜索、代理管理等功能。

## present_files — 文件展示

**功能**：将输出文件展示给用户查看和下载。

**何时使用**：
- 创建了需要用户查看的文件后
- 需要让文件在客户端界面中可见

**何时不用**：
- 仅需要内部读取文件内容
- 临时或中间文件

**安全限制**：
- 只能展示 `/mnt/user-data/outputs/` 目录下的文件
- 路径会被规范化为统一的虚拟路径格式

**工作流程**：
1. 接收文件路径列表
2. 验证路径安全性（防止路径遍历）
3. 规范化路径到虚拟格式
4. 返回 `Command` 更新 `artifacts` 状态
5. `merge_artifacts` reducer 处理合并和去重

**源文件**：`builtins/present_file_tool.py`

## ask_clarification — 澄清请求

**功能**：向用户请求澄清，中断执行流程直到用户回复。

**澄清类型**：
- `missing_info`：缺少必要信息（文件路径、URL、需求细节）
- `ambiguous_requirement`：需求有多种合理解释
- `approach_choice`：多种实现方案需用户选择
- `risk_confirmation`：危险操作需明确确认
- `suggestion`：有建议需用户批准

**工作机制**：
- 工具本身是**占位符实现**，返回固定字符串
- 实际逻辑由 `ClarificationMiddleware` 拦截处理
- `return_direct=True` 确保返回值直接传回代理

**拦截流程**：
```
代理调用 ask_clarification
    → ClarificationMiddleware 拦截
    → 中断执行
    → 展示问题给用户
    → 等待用户回复
    → 恢复执行
```

**源文件**：`builtins/clarification_tool.py`

## view_image — 图片查看

**功能**：读取图片文件并转换为 base64 编码，供视觉模型查看。

**加载条件**：仅当模型的 `supports_vision` 配置为 `True` 时加载。

**支持格式**：jpg、jpeg、png、webp

**安全限制**：
- 只能读取以下路径下的图片：
  - `/mnt/user-data/workspace`
  - `/mnt/user-data/uploads`
  - `/mnt/user-data/outputs`
- 最大文件大小：20MB
- 文件扩展名必须与实际内容格式匹配（双重验证）

**验证流程**：
1. 路径范围检查
2. 沙箱路径验证
3. 文件存在性和类型检查
4. 扩展名 MIME 类型映射
5. 文件大小检查
6. 文件头魔数验证（防止扩展名伪造）
7. Base64 编码

**状态更新**：返回 `Command` 更新 `viewed_images` 状态，由 `merge_viewed_images` reducer 处理合并。

**源文件**：`builtins/view_image_tool.py`

## task — 任务委派

**功能**：将复杂任务委派给专门的子代理执行。

**子代理类型**：
- `general-purpose`：通用代理，处理复杂多步骤任务
- `bash`：命令执行专家（需要 host bash 权限或隔离沙箱）
- 自定义类型：通过 config.yaml 的 `subagents.custom_agents` 配置

**核心机制**：
1. 在后台线程中异步执行子代理
2. 每 5 秒轮询子代理状态
3. 通过 stream writer 实时发送进度事件
4. 支持协作式取消（CancelledError）
5. 追踪并报告令牌使用量

**防嵌套设计**：子代理工具列表中 `subagent_enabled=False`，防止递归嵌套。

**流式事件**：
| 事件类型 | 说明 |
|----------|------|
| `task_started` | 任务开始 |
| `task_running` | 子代理产生新消息 |
| `task_completed` | 任务成功完成 |
| `task_failed` | 任务失败 |
| `task_cancelled` | 任务被取消 |
| `task_timed_out` | 任务超时 |

**取消流程**：
1. 收到 `CancelledError`
2. 请求协作式取消（`request_cancel_background_task`）
3. 使用 `asyncio.shield` 等待终态（确保令牌使用量快照完整）
4. 安排延迟清理（如果子代理未及时终止）

**源文件**：`builtins/task_tool.py`

## tool_search — 延迟工具搜索

**功能**：按需搜索和加载延迟工具的完整 schema。

**加载条件**：`tool_search.enabled=True` 且存在 MCP 工具。

**为什么需要**：大量 MCP 工具的完整 schema 会占用宝贵的上下文窗口。延迟加载只向模型暴露工具名称，按需加载完整定义。

**搜索模式**：
- `select:Read,Edit,Grep` — 按名称精确选择
- `+slack send` — 名称必须包含 "slack"，按剩余关键词排序
- `keyword query` — 正则匹配名称和描述

**提升机制**：
```
工具名称出现在 <available-deferred-tools>
    → 代理调用 tool_search 搜索
    → 返回匹配工具的完整 schema
    → 工具从 DeferredToolRegistry 中 promote（移除）
    → DeferredToolFilterMiddleware 不再过滤
    → 工具可在后续调用中使用
```

**请求级隔离**：使用 `contextvars.ContextVar` 存储注册表，每个异步请求有独立的注册表实例。

**源文件**：`builtins/tool_search.py`

## setup_agent — 代理创建

**功能**：引导式创建新的自定义 DeerFlow 代理。

**创建内容**：
- `SOUL.md`：代理人格和行为定义
- `config.yaml`：代理配置（名称、描述、技能白名单）

**存储位置**：
- 自定义代理：`{base_dir}/users/{user_id}/agents/{agent_name}/`
- 默认代理：`{base_dir}/SOUL.md`

**用户隔离**：每个用户的代理存储在独立目录下，不同用户之间不可见。

**错误处理**：创建失败时，如果目录是新创建的，会自动清理。

**源文件**：`builtins/setup_agent_tool.py`

## update_agent — 代理更新

**功能**：更新现有自定义代理的 SOUL.md 和 config.yaml。

**绑定条件**：仅在自定义代理的对话中可用（`runtime.context['agent_name']` 已设置）。

**可更新字段**：
| 字段 | 说明 |
|------|------|
| `soul` | SOUL.md 完整替换内容 |
| `description` | 一行描述 |
| `skills` | 技能白名单（`[]` = 禁用所有） |
| `tool_groups` | 工具组白名单 |
| `model` | 模型覆盖（需匹配配置的模型名） |

**原子写入**：采用两阶段提交：
1. 暂存阶段：所有文件先写入临时文件（.tmp）
2. 提交阶段：全部成功后使用 `Path.replace` 原子重命名

**生效时机**：更新在下一个用户回合生效。

**源文件**：`builtins/update_agent_tool.py`

## invoke_acp_agent — ACP 代理调用

**功能**：调用 ACP（Agent Client Protocol）兼容的外部代理。

**ACP 协议**：标准化的代理通信协议，允许不同代理实现之间互操作。

**调用流程**：
1. 启动 ACP 代理进程（`spawn_agent_process`）
2. 初始化连接（协议版本、客户端能力）
3. 创建会话（工作目录、MCP 服务器配置）
4. 发送提示
5. 收集流式文本响应

**每线程工作空间**：
```
{base_dir}/threads/{thread_id}/acp-workspace/
```
确保并发会话之间互不干扰。

**权限处理**：
- `auto_approve=True`：自动批准权限请求
- `auto_approve=False`（默认）：取消所有权限请求

**MCP 传递**：调用时将已启用的 MCP 服务器配置传递给 ACP 代理。

**源文件**：`builtins/invoke_acp_agent_tool.py`
