# Gateway API 接口完整参考

本文档列出 DeerFlow Gateway 的全部 HTTP 接口。所有接口通过 Nginx（端口 2026）的 `/api/*` 路径访问。

**通用约定**：
- 认证方式：HttpOnly Cookie `access_token`（JWT）
- CSRF 防护：状态变更请求需同时携带 Cookie `csrf_token` 和 Header `X-CSRF-Token`
- 内容类型：`application/json`（文件上传除外）

---

## 1. 认证接口 (`/api/v1/auth/*`)

### POST `/api/v1/auth/login/local`

本地邮箱/密码登录。

**请求体**（`application/x-www-form-urlencoded`，OAuth2 兼容格式）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `username` | string | 是 | 用户邮箱 |
| `password` | string | 是 | 密码 |

**响应** `200 LoginResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `expires_in` | int | Token 有效期（秒） |
| `needs_setup` | bool | 是否需要完成初始设置 |

**状态码**：

| 状态码 | 说明 |
|--------|------|
| 200 | 登录成功，设置 `access_token` Cookie |
| 401 | 邮箱或密码错误 |
| 429 | 登录尝试过多（5 次失败后锁定 5 分钟） |

**特殊行为**：登录成功时自动设置 CSRF Cookie；支持基于 IP 的速率限制（可通过 `AUTH_TRUSTED_PROXIES` 配置可信代理）。

---

### POST `/api/v1/auth/register`

注册新用户（角色始终为 `user`）。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `email` | EmailStr | 是 | 邮箱地址 |
| `password` | string | 是 | 密码（最少 8 位，不能为常见弱密码） |

**响应** `201 UserResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 用户 UUID |
| `email` | string | 邮箱地址 |
| `system_role` | string | 系统角色（`"user"`） |
| `needs_setup` | bool | 是否需要完成设置 |

**状态码**：

| 状态码 | 说明 |
|--------|------|
| 201 | 注册成功，自动登录 |
| 400 | 邮箱已注册 |

---

### POST `/api/v1/auth/initialize`

首次启动时创建管理员账户。仅在系统无管理员时可调用。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `email` | EmailStr | 是 | 管理员邮箱 |
| `password` | string | 是 | 密码（最少 8 位，不能为常见弱密码） |

**响应** `201 UserResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 用户 UUID |
| `email` | string | 邮箱地址 |
| `system_role` | string | `"admin"` |
| `needs_setup` | bool | `false`（初始管理员直接就绪） |

**状态码**：

| 状态码 | 说明 |
|--------|------|
| 201 | 管理员创建成功，自动登录 |
| 409 | 系统已初始化（管理员已存在） |

---

### GET `/api/v1/auth/setup-status`

检查系统是否需要初始化（是否存在管理员账户）。

**响应** `200`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `needs_setup` | bool | `true` 表示无管理员，需要初始化 |

---

### GET `/api/v1/auth/me`

获取当前认证用户信息。

**响应** `200 UserResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 用户 UUID |
| `email` | string | 邮箱地址 |
| `system_role` | string | 系统角色 |
| `needs_setup` | bool | 是否需要完成设置 |

**状态码**：401 未认证

---

### POST `/api/v1/auth/change-password`

修改当前用户密码。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `current_password` | string | 是 | 当前密码 |
| `new_password` | string | 是 | 新密码（最少 8 位，不能为常见弱密码） |
| `new_email` | EmailStr | 否 | 同时更新邮箱 |

**响应** `200 MessageResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `message` | string | `"Password changed successfully"` |

**状态码**：

| 状态码 | 说明 |
|--------|------|
| 200 | 密码修改成功，重新签发 Cookie |
| 400 | 当前密码错误 / 邮箱已使用 / OAuth 用户不可修改密码 |

**特殊行为**：修改密码后 `token_version` 递增，使所有旧 Token 失效。如提供 `new_email` 且 `needs_setup=true`，将清除设置标记。

---

### POST `/api/v1/auth/logout`

登出当前用户。

**响应** `200 MessageResponse`：清除 `access_token` Cookie。

---

## 2. 模型接口 (`/api/models`)

### GET `/api/models`

获取所有可用模型列表。

**响应** `200 ModelsListResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `models` | ModelResponse[] | 模型列表 |
| `token_usage` | TokenUsageResponse | Token 用量显示配置 |

**ModelResponse 字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 模型唯一标识 |
| `model` | string | 实际 provider 模型标识 |
| `display_name` | string? | 显示名称 |
| `description` | string? | 模型描述 |
| `supports_thinking` | bool | 是否支持思考模式 |
| `supports_reasoning_effort` | bool | 是否支持推理努力调节 |

---

### GET `/api/models/{model_name}`

获取指定模型详情。

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `model_name` | string | 模型名称 |

**状态码**：200 成功 | 404 模型不存在

---

## 3. MCP 接口 (`/api/mcp`)

### GET `/api/mcp/config`

获取当前 MCP 服务器配置。

**响应** `200 McpConfigResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `mcp_servers` | dict\<string, McpServerConfigResponse\> | 服务器名称到配置的映射 |

**McpServerConfigResponse 字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `enabled` | bool | 是否启用 |
| `type` | string | 传输类型：`"stdio"` / `"sse"` / `"http"` |
| `command` | string? | 启动命令（stdio 类型） |
| `args` | string[] | 命令参数 |
| `env` | dict\<string, string\> | 环境变量 |
| `url` | string? | 服务器 URL（sse/http 类型） |
| `headers` | dict\<string, string\> | HTTP 请求头 |
| `oauth` | McpOAuthConfigResponse? | OAuth 配置 |
| `description` | string | 服务器描述 |

---

### PUT `/api/mcp/config`

更新 MCP 服务器配置。保存到 `extensions_config.json` 并重载缓存。

**请求体** `McpConfigUpdateRequest`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `mcp_servers` | dict\<string, McpServerConfigResponse\> | 是 | 完整的服务器配置映射 |

**响应** `200 McpConfigResponse`：更新后的完整配置。

**状态码**：200 成功 | 500 写入配置文件失败

---

## 4. 技能接口 (`/api/skills`)

### GET `/api/skills`

获取所有技能列表。

**响应** `200 SkillsListResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `skills` | SkillResponse[] | 技能列表 |

**SkillResponse 字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 技能名称 |
| `description` | string | 技能描述 |
| `license` | string? | 许可证信息 |
| `category` | string | 类别：`"public"` / `"custom"` |
| `enabled` | bool | 是否启用 |

---

### GET `/api/skills/{skill_name}`

获取指定技能详情。

**路径参数**：`skill_name` — 技能名称

**状态码**：200 成功 | 404 技能不存在

---

### PUT `/api/skills/{skill_name}`

更新技能启用状态。修改 `extensions_config.json` 并重载缓存。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `enabled` | bool | 是 | 启用或禁用 |

**状态码**：200 成功 | 404 技能不存在

---

### POST `/api/skills/install`

从 `.skill` 归档文件安装技能。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `thread_id` | string | 是 | `.skill` 文件所在线程 ID |
| `path` | string | 是 | `.skill` 文件虚拟路径 |

**响应** `200 SkillInstallResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | bool | 是否安装成功 |
| `skill_name` | string | 已安装技能名称 |
| `message` | string | 安装结果消息 |

**状态码**：200 成功 | 400 无效归档 | 404 文件未找到 | 409 技能已存在

---

### GET `/api/skills/custom`

列出所有自定义技能。

**响应** `200 SkillsListResponse`。

---

### GET `/api/skills/custom/{skill_name}`

获取自定义技能内容（含 SKILL.md 原文）。

**响应** `200 CustomSkillContentResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `content` | string | SKILL.md 原始内容 |
| ... | ... | 继承 SkillResponse 所有字段 |

---

### PUT `/api/skills/custom/{skill_name}`

编辑自定义技能 SKILL.md 内容。执行安全扫描后写入。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | 新的 SKILL.md 内容 |

**状态码**：200 成功 | 400 安全扫描拒绝 | 404 技能不存在

---

### DELETE `/api/skills/custom/{skill_name}`

删除自定义技能。

**响应** `200`：`{"success": true}`

---

### GET `/api/skills/custom/{skill_name}/history`

获取自定义技能编辑历史。

**响应** `200 CustomSkillHistoryResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `history` | list\<dict\> | 编辑历史记录列表 |

---

### POST `/api/skills/custom/{skill_name}/rollback`

回滚自定义技能到指定历史版本。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `history_index` | int | 否 | 历史条目索引（默认 -1，即最近一条） |

**状态码**：200 成功 | 400 无历史/索引越界/安全扫描拒绝 | 404 技能不存在

---

## 5. 记忆接口 (`/api/memory`)

### GET `/api/memory`

获取当前用户的记忆数据。

**响应** `200 MemoryResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `version` | string | 记忆数据版本（`"1.0"`） |
| `lastUpdated` | string | 最后更新时间 |
| `user` | UserContext | 用户上下文 |
| `history` | HistoryContext | 历史上下文 |
| `facts` | Fact[] | 事实列表 |

**UserContext**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `workContext` | ContextSection | 工作上下文 |
| `personalContext` | ContextSection | 个人上下文 |
| `topOfMind` | ContextSection | 当前关注点 |

**ContextSection**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `summary` | string | 摘要内容 |
| `updatedAt` | string | 更新时间 |

**Fact**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 事实唯一标识 |
| `content` | string | 事实内容 |
| `category` | string | 类别（preference/knowledge/context/behavior/goal） |
| `confidence` | float | 置信度（0-1） |
| `createdAt` | string | 创建时间 |
| `source` | string | 来源线程 ID |

---

### POST `/api/memory/reload`

强制从文件重新加载记忆数据。

**响应** `200 MemoryResponse`。

---

### DELETE `/api/memory`

清除所有记忆数据并重置为空结构。

**响应** `200 MemoryResponse`。

---

### POST `/api/memory/facts`

手动创建一条记忆事实。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | 事实内容（最少 1 字符） |
| `category` | string | 否 | 类别（默认 `"context"`） |
| `confidence` | float | 否 | 置信度（0-1，默认 0.5） |

**状态码**：200 成功 | 400 内容为空/置信度无效

---

### PATCH `/api/memory/facts/{fact_id}`

部分更新指定记忆事实。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string? | 否 | 新内容 |
| `category` | string? | 否 | 新类别 |
| `confidence` | float? | 否 | 新置信度（0-1） |

**状态码**：200 成功 | 404 事实不存在 | 400 内容无效

---

### DELETE `/api/memory/facts/{fact_id}`

删除指定记忆事实。

**状态码**：200 成功 | 404 事实不存在

---

### GET `/api/memory/export`

导出记忆数据（与 GET `/api/memory` 等价）。

**响应** `200 MemoryResponse`。

---

### POST `/api/memory/import`

导入并覆盖当前记忆数据。

**请求体**：`MemoryResponse` 完整结构。

**状态码**：200 成功 | 500 写入失败

---

### GET `/api/memory/config`

获取记忆系统配置。

**响应** `200 MemoryConfigResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `enabled` | bool | 是否启用记忆系统 |
| `storage_path` | string | 存储文件路径 |
| `debounce_seconds` | int | 防抖延迟（秒） |
| `max_facts` | int | 最大事实数量 |
| `fact_confidence_threshold` | float | 事实最低置信度阈值 |
| `injection_enabled` | bool | 是否启用记忆注入 |
| `max_injection_tokens` | int | 注入最大 Token 数 |

---

### GET `/api/memory/status`

获取记忆系统状态（配置 + 数据）。

**响应** `200 MemoryStatusResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `config` | MemoryConfigResponse | 记忆配置 |
| `data` | MemoryResponse | 记忆数据 |

---

## 6. 线程接口 (`/api/threads/*`)

### POST `/api/threads`

创建新线程。

**请求体** `ThreadCreateRequest`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `thread_id` | string? | 否 | 自定义线程 ID（默认自动生成） |
| `assistant_id` | string? | 否 | 关联的助手 ID |
| `metadata` | dict? | 否 | 初始元数据 |

**响应** `201 ThreadResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `thread_id` | string | 线程唯一标识 |
| `status` | string | 线程状态（`"idle"`） |
| `created_at` | string | 创建时间（ISO） |
| `updated_at` | string | 更新时间（ISO） |
| `metadata` | dict | 元数据 |
| `values` | dict | 当前状态值 |
| `interrupts` | dict | 待处理中断 |

**特殊行为**：幂等操作 — 若 `thread_id` 已存在则返回已有记录。

---

### POST `/api/threads/search`

搜索线程列表。

**请求体** `ThreadSearchRequest`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `metadata` | dict? | 否 | 元数据精确过滤 |
| `limit` | int | 否 | 最大结果数（1-1000，默认 100） |
| `offset` | int | 否 | 分页偏移 |
| `status` | string? | 否 | 按状态过滤 |

**响应** `200 list[ThreadResponse]`。

---

### GET `/api/threads/{thread_id}`

获取线程详情。从 ThreadMetaStore 读取元数据，从 Checkpointer 推导执行状态。

**路径参数**：`thread_id` — 线程 ID

**响应** `200 ThreadResponse`。包含从检查点推导的准确状态（idle/busy/interrupted/error）。

**状态码**：200 成功 | 404 线程不存在

---

### PATCH `/api/threads/{thread_id}`

合并更新线程元数据。

**请求体** `ThreadPatchRequest`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `metadata` | dict | 否 | 要合并的元数据 |

**状态码**：200 成功 | 404 线程不存在

---

### DELETE `/api/threads/{thread_id}`

删除线程及其所有本地数据。

**响应** `200 ThreadDeleteResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | bool | 是否成功 |
| `message` | string | 结果消息 |

**特殊行为**：依次执行——删除本地文件目录 → 删除检查点数据（best-effort）→ 删除 thread_meta 行（best-effort）。

---

### GET `/api/threads/{thread_id}/state`

获取线程最新状态快照。

**响应** `200 ThreadStateResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `values` | dict | 当前 channel values（消息已序列化） |
| `next` | string[] | 待执行的下一步任务 |
| `metadata` | dict | 检查点元数据 |
| `checkpoint` | dict | 检查点信息 |
| `checkpoint_id` | string? | 当前检查点 ID |
| `parent_checkpoint_id` | string? | 父检查点 ID |
| `created_at` | string? | 检查点时间 |
| `tasks` | list\<dict\> | 中断的任务详情 |

---

### POST `/api/threads/{thread_id}/state`

更新线程状态（用于人机交互恢复或标题重命名）。

**请求体** `ThreadStateUpdateRequest`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `values` | dict? | 否 | 要合并的 channel values |
| `checkpoint_id` | string? | 否 | 从指定检查点分支 |
| `checkpoint` | dict? | 否 | 完整检查点对象 |
| `as_node` | string? | 否 | 更新的节点标识 |

**特殊行为**：如 `values` 包含 `title` 字段，自动同步到 ThreadMetaStore。

---

### POST `/api/threads/{thread_id}/history`

获取线程检查点历史。

**请求体** `ThreadHistoryRequest`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `limit` | int | 否 | 最大条目数（1-100，默认 10） |
| `before` | string? | 否 | 分页游标 |

**响应** `200 list[HistoryEntry]`：检查点历史列表。仅最新条目包含 `messages` 字段以避免重复。

---

### GET `/api/threads/{thread_id}/messages`

获取线程跨所有运行的消息列表，附带反馈信息。

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 50 | 最大消息数（<=200） |
| `before_seq` | int? | - | 向前分页游标 |
| `after_seq` | int? | - | 向后分页游标 |

**响应** `200 list[dict]`：每条消息附带 `feedback` 字段（来自最后一个 AI 消息的反馈）。

---

### GET `/api/threads/{thread_id}/token-usage`

获取线程级别的 Token 用量聚合。

**响应** `200 ThreadTokenUsageResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `thread_id` | string | 线程 ID |
| `total_tokens` | int | 总 Token 数 |
| `total_input_tokens` | int | 总输入 Token 数 |
| `total_output_tokens` | int | 总输出 Token 数 |
| `total_runs` | int | 总运行次数 |
| `by_model` | dict | 按模型分类的 Token 统计 |
| `by_caller` | dict | 按调用者分类（lead_agent/subagent/middleware） |

---

## 7. 上传接口 (`/api/threads/{id}/uploads`)

### POST `/api/threads/{thread_id}/uploads`

上传文件到线程目录。

**请求体**：`multipart/form-data`，字段 `files` 可包含多个文件。

**响应** `200 UploadResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | bool | 是否全部上传成功 |
| `files` | list\<dict\> | 已上传文件列表 |
| `message` | string | 结果消息 |
| `skipped_files` | list\<string\> | 被跳过的不安全文件 |

**files 元素字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `filename` | string | 文件名 |
| `size` | string | 文件大小（字节） |
| `path` | string | 沙箱相对路径 |
| `virtual_path` | string | 虚拟路径 |
| `artifact_url` | string | 产物访问 URL |

**状态码**：200 成功（可能包含跳过文件）| 400 无文件 | 413 文件过大

**特殊行为**：
- 支持 PDF/PPT/Excel/Word 自动转换为 Markdown（需 `uploads.auto_convert_documents` 配置）
- 重复文件名自动添加 `_N` 后缀
- 默认限制：最多 10 个文件，单文件 50MB，总计 100MB

---

### GET `/api/threads/{thread_id}/uploads/list`

列出线程上传目录中的所有文件。

**响应** `200 dict`：`{"files": [...], "count": N}`

---

### GET `/api/threads/{thread_id}/uploads/limits`

获取上传限制配置。

**响应** `200 UploadLimits`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `max_files` | int | 最大文件数 |
| `max_file_size` | int | 单文件最大字节数 |
| `max_total_size` | int | 总上传最大字节数 |

---

### DELETE `/api/threads/{thread_id}/uploads/{filename}`

删除指定上传文件。

**路径参数**：`thread_id`、`filename`

**状态码**：200 成功 | 400 路径无效 | 404 文件不存在

---

## 8. 产物接口 (`/api/threads/{id}/artifacts`)

### GET `/api/threads/{thread_id}/artifacts/{path:path}`

获取 Agent 生成的产物文件。

**路径参数**：

| 参数 | 说明 |
|------|------|
| `thread_id` | 线程 ID |
| `path` | 产物虚拟路径（如 `mnt/user-data/outputs/file.txt`） |

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `download` | bool | false | 强制下载 |

**特殊行为**：
- HTML/XHTML/SVG 等活动内容类型 **始终** 以附件形式下载（防 XSS）
- 支持 `.skill` 归档文件内部文件访问（如 `xxx.skill/SKILL.md`）
- 文本文件以 `text/plain` 返回，二进制文件内联显示

**状态码**：200 成功 | 400 路径无效 | 403 路径穿越 | 404 文件不存在

---

## 9. 线程运行接口 (`/api/threads/{id}/runs`)

### POST `/api/threads/{thread_id}/runs`

创建后台运行（立即返回）。

**请求体** `RunCreateRequest`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `assistant_id` | string? | 否 | 使用的代理 ID |
| `input` | dict? | 否 | 图输入（如 `{messages: [...]}`） |
| `metadata` | dict? | 否 | 运行元数据 |
| `config` | dict? | 否 | RunnableConfig 覆盖 |
| `context` | dict? | 否 | DeerFlow 上下文覆盖（model_name, thinking_enabled 等） |
| `stream_mode` | string/list? | 否 | 流模式 |
| `stream_subgraphs` | bool | 否 | 包含子图事件（默认 false） |
| `on_disconnect` | string | 否 | 断开行为：`"cancel"` / `"continue"`（默认 cancel） |
| `multitask_strategy` | string | 否 | 并发策略：`reject`/`rollback`/`interrupt`/`enqueue` |
| `interrupt_before` | list? | 否 | 前置中断节点 |
| `interrupt_after` | list? | 否 | 后置中断节点 |

**响应** `200 RunResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `run_id` | string | 运行 ID |
| `thread_id` | string | 线程 ID |
| `assistant_id` | string? | 代理 ID |
| `status` | string | 运行状态 |
| `metadata` | dict | 运行元数据 |
| `created_at` | string | 创建时间 |

**状态码**：200 成功 | 409 并发冲突 | 501 不支持的多任务策略

---

### POST `/api/threads/{thread_id}/runs/stream`

创建运行并通过 SSE 流式返回事件。

**请求体**：同 `RunCreateRequest`。

**响应**：`text/event-stream` SSE 流。

**SSE 事件格式**：

```
event: <event_type>
data: <json_payload>

```

事件类型包括：`values`、`messages-tuple`、`custom`、`end`。心跳帧：`: heartbeat\n\n`。

**特殊行为**：响应包含 `Content-Location` 头，值为运行资源 URL，供 LangGraph SDK 提取运行元数据。

---

### POST `/api/threads/{thread_id}/runs/wait`

创建运行并阻塞等待完成，返回最终状态。

**请求体**：同 `RunCreateRequest`。

**响应** `200 dict`：最终检查点的 channel values（已序列化）。

---

### GET `/api/threads/{thread_id}/runs`

列出线程的所有运行。

**响应** `200 list[RunResponse]`。

---

### GET `/api/threads/{thread_id}/runs/{run_id}`

获取指定运行的详情。

**状态码**：200 成功 | 404 运行不存在

---

### POST `/api/threads/{thread_id}/runs/{run_id}/cancel`

取消正在运行的运行。

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `wait` | bool | false | 是否等待取消完成 |
| `action` | string | `"interrupt"` | 取消动作：`"interrupt"` / `"rollback"` |

**状态码**：202 已接受取消 | 204 等待取消完成 | 404 运行不存在 | 409 无法取消

---

### GET `/api/threads/{thread_id}/runs/{run_id}/join`

加入现有运行的 SSE 流。

**响应**：`text/event-stream` SSE 流。

**状态码**：200 成功 | 404 运行不存在 | 409 运行不在当前 worker

---

### GET/POST `/api/threads/{thread_id}/runs/{run_id}/stream`

加入现有 SSE 流（GET），或取消后流式（POST）。

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `action` | string? | - | 取消动作（POST 时使用） |
| `wait` | int | 0 | 是否等待取消完成 |

**状态码**：200 SSE 流 | 204 取消完成 | 404 运行不存在 | 409 无法操作

---

### GET `/api/threads/{thread_id}/runs/{run_id}/messages`

获取指定运行的消息（分页）。

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 50 | 最大条目数（1-200） |
| `before_seq` | int? | - | 前向游标 |
| `after_seq` | int? | - | 后向游标 |

**响应** `200`：`{"data": [...], "has_more": bool}`

---

### GET `/api/threads/{thread_id}/runs/{run_id}/events`

获取指定运行的完整事件流（调试/审计用）。

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `event_types` | string? | - | 事件类型过滤（逗号分隔） |
| `limit` | int | 500 | 最大条目数（<=2000） |

**响应** `200 list[dict]`。

---

## 10. 无状态运行 (`/api/runs`)

### POST `/api/runs/stream`

无状态运行 — 自动创建临时线程，通过 SSE 流式返回。如 `config.configurable.thread_id` 已提供则复用已有线程。

**请求体**：同 `RunCreateRequest`。

**响应**：`text/event-stream` SSE 流，包含 `Content-Location` 头。

---

### POST `/api/runs/wait`

无状态运行 — 创建并阻塞等待完成。

**请求体**：同 `RunCreateRequest`。

**响应** `200 dict`：最终状态。

---

### GET `/api/runs/{run_id}/messages`

按 run_id 获取消息（分页）。

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 50 | 最大条目数（1-200） |
| `before_seq` | int? | - | 前向游标 |
| `after_seq` | int? | - | 后向游标 |

**响应** `200`：`{"data": [...], "has_more": bool}`

---

### GET `/api/runs/{run_id}/feedback`

按 run_id 获取反馈列表。

**响应** `200 list[dict]`。

---

## 11. 反馈接口 (`/api/threads/{id}/runs/{rid}/feedback`)

### PUT `/api/threads/{thread_id}/runs/{run_id}/feedback`

创建或更新运行反馈（幂等操作）。

**请求体** `FeedbackUpsertRequest`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `rating` | int | 是 | 评分：`+1`（正面）或 `-1`（负面） |
| `comment` | string? | 否 | 文字反馈 |

**状态码**：200 成功 | 400 评分无效 | 404 运行不存在

---

### POST `/api/threads/{thread_id}/runs/{run_id}/feedback`

提交运行反馈。

**请求体** `FeedbackCreateRequest`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `rating` | int | 是 | 评分：`+1` 或 `-1` |
| `comment` | string? | 否 | 文字反馈 |
| `message_id` | string? | 否 | 关联到特定消息 |

**状态码**：200 成功 | 400 评分无效 | 404 运行不存在

---

### DELETE `/api/threads/{thread_id}/runs/{run_id}/feedback`

删除当前用户对该运行的所有反馈。

**状态码**：200 成功 | 404 无反馈

---

### GET `/api/threads/{thread_id}/runs/{run_id}/feedback`

列出运行的所有反馈。

**响应** `200 list[FeedbackResponse]`。

---

### GET `/api/threads/{thread_id}/runs/{run_id}/feedback/stats`

获取运行反馈的聚合统计。

**响应** `200 FeedbackStatsResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `run_id` | string | 运行 ID |
| `total` | int | 总反馈数 |
| `positive` | int | 正面反馈数 |
| `negative` | int | 负面反馈数 |

---

### DELETE `/api/threads/{thread_id}/runs/{run_id}/feedback/{feedback_id}`

删除指定反馈记录。

**状态码**：200 成功 | 404 反馈不存在

---

## 12. 建议接口 (`/api/threads/{id}/suggestions`)

### POST `/api/threads/{thread_id}/suggestions`

基于对话上下文生成后续问题建议。

**请求体** `SuggestionsRequest`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `messages` | SuggestionMessage[] | 是 | 最近对话消息 |
| `n` | int | 否 | 建议数量（1-5，默认 3） |
| `model_name` | string? | 否 | 可选模型覆盖 |

**SuggestionMessage**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `role` | string | 角色：`user` / `assistant` |
| `content` | string | 消息内容 |

**响应** `200 SuggestionsResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `suggestions` | string[] | 建议的后续问题列表 |

---

## 13. 代理接口 (`/api/agents`)

所有代理接口需要 `agents_api.enabled=true` 配置。

### GET `/api/agents`

列出所有自定义代理。

**响应** `200 AgentsListResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `agents` | AgentResponse[] | 代理列表 |

**AgentResponse 字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 代理名称（连字符格式） |
| `description` | string | 代理描述 |
| `model` | string? | 可选模型覆盖 |
| `tool_groups` | string[]? | 可选工具组白名单 |
| `skills` | string[]? | 可选技能白名单 |
| `soul` | string? | SOUL.md 内容 |

---

### GET `/api/agents/{name}`

获取指定代理详情。

**路径参数**：`name` — 代理名称

**状态码**：200 成功 | 404 代理不存在

---

### POST `/api/agents`

创建新自定义代理。

**请求体** `AgentCreateRequest`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 代理名称（仅字母、数字、连字符） |
| `description` | string | 否 | 代理描述 |
| `model` | string? | 否 | 模型覆盖 |
| `tool_groups` | string[]? | 否 | 工具组白名单 |
| `skills` | string[]? | 否 | 技能白名单 |
| `soul` | string | 否 | SOUL.md 内容 |

**状态码**：201 创建成功 | 409 代理已存在 | 422 名称无效

---

### PUT `/api/agents/{name}`

更新现有自定义代理。

**请求体** `AgentUpdateRequest`（所有字段可选）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `description` | string? | 更新描述 |
| `model` | string? | 更新模型覆盖 |
| `tool_groups` | string[]? | 更新工具组白名单 |
| `skills` | string[]? | 更新技能白名单 |
| `soul` | string? | 更新 SOUL.md 内容 |

**状态码**：200 成功 | 404 代理不存在 | 409 仅存在遗留布局（需迁移）

---

### DELETE `/api/agents/{name}`

删除自定义代理及其所有文件。

**状态码**：204 删除成功 | 404 代理不存在 | 409 仅存在遗留布局

---

### GET `/api/agents/check?name={name}`

检查代理名称是否可用。

**查询参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `name` | string | 要检查的名称 |

**响应** `200`：`{"available": bool, "name": "<normalized>"}`

---

### GET `/api/user-profile`

获取全局用户配置文件（USER.md）。

**响应** `200 UserProfileResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `content` | string? | USER.md 内容，`null` 表示尚未创建 |

---

### PUT `/api/user-profile`

更新全局用户配置文件。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 否 | USER.md 内容 |

**响应** `200 UserProfileResponse`。

---

## 14. 通道接口 (`/api/channels`)

### GET `/api/channels/`

获取所有 IM 通道状态。

**响应** `200 ChannelStatusResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `service_running` | bool | 通道服务是否运行 |
| `channels` | dict\<string, dict\> | 各通道状态 |

---

### POST `/api/channels/{name}/restart`

重启指定 IM 通道。

**路径参数**：`name` — 通道名称

**响应** `200 ChannelRestartResponse`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | bool | 是否成功 |
| `message` | string | 结果消息 |

**状态码**：200 成功 | 503 通道服务未运行

---

## 15. 兼容接口 (`/api/assistants/*`)

LangGraph Platform 兼容层，满足 `useStream` React Hook 的初始化需求。

### POST `/api/assistants/search`

搜索助手列表。返回 `lead_agent` + 所有自定义代理。

**请求体** `AssistantSearchRequest`（可选）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `graph_id` | string? | 按图 ID 过滤 |
| `name` | string? | 按名称模糊搜索 |
| `limit` | int | 最大结果数（默认 10） |
| `offset` | int | 分页偏移 |

**响应** `200 list[AssistantResponse]`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `assistant_id` | string | 助手 ID |
| `graph_id` | string | 图 ID |
| `name` | string | 显示名称 |
| `config` | dict | 配置 |
| `metadata` | dict | 元数据 |
| `description` | string? | 描述 |

---

### GET `/api/assistants/{assistant_id}`

获取指定助手信息。

**状态码**：200 成功 | 404 助手不存在

---

### GET `/api/assistants/{assistant_id}/graph`

获取助手的图结构（最小存根）。

**响应** `200`：`{"graph_id": "lead_agent", "nodes": [], "edges": []}`

---

### GET `/api/assistants/{assistant_id}/schemas`

获取助手的输入/输出/状态 JSON Schema（空 Schema 存根）。

**响应** `200`：

```json
{
  "graph_id": "lead_agent",
  "input_schema": {},
  "output_schema": {},
  "state_schema": {},
  "config_schema": {}
}
```

---

## 16. 健康检查

### GET `/health`

**响应** `200`：

```json
{
  "status": "healthy",
  "service": "deer-flow-gateway"
}
```

无需认证。
