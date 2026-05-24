# Channels 运行时能力清单与来源

> 完整列举 Channels 系统在运行时的所有能力和功能特性，标注每个能力的
> 实现来源（代码文件、配置项、外部依赖）。

## 核心调度能力

### 1. 消息路由与线程管理

| 能力 | 描述 | 来源 |
|------|------|------|
| IM 会话 → DeerFlow 线程映射 | 通过 ChannelStore 持久化映射关系 | `store.py`, `manager.py:_handle_chat` |
| 线程复用 | 同一 chat_id+topic_id 的消息复用同一 thread_id | `manager.py:_handle_chat` L747-756 |
| 新线程创建 | 调用 `client.threads.create()` 通过 Gateway API 创建 | `manager.py:_create_thread` |
| 多级键结构 | 支持 channel:chat_id 和 channel:chat_id:topic_id | `store.py:_key` |
| 线程映射删除 | 支持按 topic_id 精确删除或批量删除 | `store.py:remove` |

### 2. Agent 调用

| 能力 | 描述 | 来源 |
|------|------|------|
| 非流式调用 | `client.runs.wait()` — 阻塞等待 Agent 完成 | `manager.py:_handle_chat` L791-798 |
| 流式调用 | `client.runs.stream()` — 实时推送文本增量 | `manager.py:_handle_streaming_chat` |
| 并发控制 | `asyncio.Semaphore` 限制同时处理数 (默认 5) | `manager.py:__init__` L571 |
| 线程忙碌检测 | `multitask_strategy="reject"` + ConflictError 检测 | `manager.py:_handle_chat` L799-803 |
| internal auth | 内网认证头部，绕过 Gateway 认证 | `manager.py:_get_client` L644-648 |
| CSRF 保护 | 生成 CSRF token 并加入请求头/Cookie | `manager.py:__init__` L570 |

### 3. 响应处理

| 能力 | 描述 | 来源 |
|------|------|------|
| 响应文本提取 | 从 LangGraph 状态中提取最后 AI 消息文本 | `manager.py:_extract_response_text` |
| 流式文本累积 | 合并 delta 文本和全量快照 | `manager.py:_accumulate_stream_text` |
| Artifact 提取 | 从 `present_files` tool call 提取文件路径 | `manager.py:_extract_artifacts` |
| Artifact 解析 | 虚拟路径 → 本地文件系统路径 + MIME + 大小 | `manager.py:_resolve_attachments` |
| 附件安全校验 | 仅允许 `/mnt/user-data/outputs/` 下的路径 | `manager.py:_resolve_attachments` L374 |
| 路径遍历防护 | 验证解析后的路径确实在 outputs 目录下 | `manager.py:_resolve_attachments` L381-385 |
| 文件名回退 | 解析失败时在文本中展示文件名 | `manager.py:_prepare_artifact_delivery` |

### 4. 会话配置

| 能力 | 描述 | 来源 |
|------|------|------|
| 四层配置合并 | 默认 → 全局 session → channel session → user session | `manager.py:_resolve_run_params` |
| assistant_id 覆盖 | 各级配置可覆盖使用的 Agent | `manager.py:_resolve_session_layer` |
| 自定义 Agent 名称 | 非 lead_agent 的 agent 通过 agent_name context 路由 | `manager.py:_resolve_run_params` L628-630 |
| recursion_limit 覆盖 | 各级可覆盖递归限制 | `manager.py:_merge_dicts` |
| context 覆盖 | thinking_enabled / is_plan_mode / subagent_enabled | `manager.py:DEFAULT_RUN_CONTEXT` |

## 频道平台能力

### 5. 飞书/Lark 能力

| 能力 | 描述 | 来源 |
|------|------|------|
| WebSocket 长连接 | lark-oapi WS 客户端 | `feishu.py:_run_ws` |
| 流式卡片更新 | PatchMessage API 实时更新交互式卡片 | `feishu.py:_update_card` |
| 运行卡片追踪 | 创建 running card 并在流式输出时更新 | `feishu.py:_running_card_ids` |
| 表情反应 | OK (收到) / DONE (完成) | `feishu.py:_add_reaction` |
| 图片下载 | 从消息中提取 image_key → 下载 → 写入沙箱 | `feishu.py:receive_file` |
| 文件下载 | 从消息中提取 file_key → 下载 → 写入沙箱 | `feishu.py:receive_file` |
| 沙箱文件同步 | 下载后同步到非 local 沙箱 | `feishu.py:receive_file` L381-392 |
| 富文本解析 | 解析 rich-text paragraph 含 @mention / 图片 / 文件 | `feishu.py:_on_message` L623-654 |
| 图片上传 | image.create API (≤10MB) | `feishu.py:_upload_image` |
| 文件上传 | file.create API (≤30MB, 自动检测文件类型) | `feishu.py:_upload_file` |
| 多域名支持 | 飞书中国 / Lark 国际 | `feishu.py:start` L114 |

### 6. 钉钉能力

| 能力 | 描述 | 来源 |
|------|------|------|
| Stream Push WebSocket | dingtalk-stream SDK | `dingtalk.py:_run_stream` |
| AI Card 流式 | AICardReplier 创建和流式更新卡片 | `dingtalk.py:_create_and_deliver_card` |
| AI Card 回退 | Card 失败时回退到 sampleMarkdown | `dingtalk.py:send` L238-248 |
| Markdown 适配 | 代码块→引用、内联代码→粗体、表格→键值对 | `dingtalk.py:_adapt_markdown_for_dingtalk` |
| Access Token 缓存 | 带过期时间的 token 缓存 (提前 5 分钟刷新) | `dingtalk.py:_get_access_token` |
| P2P 消息 | sampleMarkdown 发送给个人 | `dingtalk.py:_send_p2p_message` |
| 群聊消息 | sampleMarkdown 发送到群 | `dingtalk.py:_send_group_message` |
| 媒体上传 | image/file 通过 files/upload API | `dingtalk.py:_upload_media` |
| 运行提示 | AI Card 或 sampleText "Working on it..." | `dingtalk.py:_send_running_reply` |
| 用户过滤 | allowed_users 白名单 | `dingtalk.py:_on_chatbot_message` L382-384 |
| 重试机制 | 指数退避 (1s, 2s) 最多 3 次 | `dingtalk.py:send` L251-275 |

### 7. 企业微信能力

| 能力 | 描述 | 来源 |
|------|------|------|
| WebSocket 连接 | aibot WSClient | `wecom.py:start` |
| 流式回复 | reply_stream API 实时更新消息 | `wecom.py:_send_ws` |
| 文本消息 | message.text 事件 | `wecom.py:_on_ws_text` |
| 混合消息 | message.mixed 事件 (文本+图片+文件) | `wecom.py:_on_ws_mixed` |
| 图片消息 | message.image 事件 (含 URL + AES 密钥) | `wecom.py:_on_ws_image` |
| 文件消息 | message.file 事件 (含 URL + AES 密钥) | `wecom.py:_on_ws_file` |
| 文件解密 | aibot.crypto_utils.decrypt_file | `manager.py:_read_wecom_inbound_file` |
| 分块上传 | init → chunk(512KB) → finish 三阶段 | `wecom.py:_upload_media_ws` |
| 图片限制 | ≤2MB | `wecom.py:send_file` L147 |
| 文件限制 | ≤20MB | `wecom.py:send_file` L147 |
| WS 上下文管理 | frame/stream_id 按 thread_ts 追踪 | `wecom.py:_ws_frames/_ws_stream_ids` |

### 8. Slack 能力

| 能力 | 描述 | 来源 |
|------|------|------|
| Socket Mode | slack-sdk SocketModeClient | `slack.py:start` |
| md→mrkdwn 转换 | markdown_to_mrkdwn 格式转换 | `slack.py:send` |
| 表情反应 | eyes(收到) / white_check_mark(成功) / x(失败) | `slack.py:_add_reaction` |
| 运行提示 | hourglass "Working on it..." 在线程中 | `slack.py:_send_running_reply` |
| 文件上传 | files_upload_v2 API | `slack.py:send_file` |
| 用户过滤 | allowed_users 白名单 | `slack.py:_normalize_allowed_users` |
| 重试机制 | 指数退避 最多 3 次 | `slack.py:send` L110-149 |

### 9. Discord 能力

| 能力 | 描述 | 来源 |
|------|------|------|
| WebSocket 客户端 | discord.py Client | `discord.py:start` |
| 线程创建/复用 | 为每个频道创建 Discord Thread | `discord.py:_create_thread` |
| mention_only 模式 | 仅在被 @时回复 | `discord.py:_on_message` |
| thread_mode 模式 | 自动创建线程组织对话 | `discord.py:_on_message` L388-403 |
| 允许频道 | allowed_channels 白名单 | `discord.py:__init__` L45-47 |
| Guild 过滤 | allowed_guilds 服务器白名单 | `discord.py:__init__` L38-42 |
| 输入状态 | trigger_typing 每 10 秒 | `discord.py:_start_typing` |
| 消息拆分 | 2000 字符限制，换行处拆分 | `discord.py:_split_text` |
| 表情确认 | ✅ checkmark 反应 | `discord.py:_add_reaction` |
| 文件上传 | discord.File 附件 | `discord.py:send_file` |
| 线程持久化 | discord_threads.json | `discord.py:_save_thread` |

### 10. Telegram 能力

| 能力 | 描述 | 来源 |
|------|------|------|
| 长轮询 | python-telegram-bot Application | `telegram.py:_run_polling` |
| 命令处理 | /start /new /status /models /memory /help | `telegram.py:start` |
| 消息线程 | reply_to_message_id 关联 | `telegram.py:send` |
| 运行提示 | "Working on it..." 回复 | `telegram.py:_send_running_reply` |
| 图片发送 | send_photo (≤10MB) | `telegram.py:send_file` L151-156 |
| 文件发送 | send_document (≤50MB) | `telegram.py:send_file` L158-165 |
| 用户过滤 | allowed_users 白名单 (int user_id) | `telegram.py:__init__` L30-35 |
| 线程隔离 | 独立线程 + 独立事件循环 | `telegram.py:_run_polling` |
| 重试机制 | 指数退避 最多 3 次 | `telegram.py:send` L109-130 |
| 私聊/群聊路由 | 私聊共享线程, 群聊按消息分离 | `telegram.py:_on_text` L290-302 |

### 11. 微信 iLink 能力

| 能力 | 描述 | 来源 |
|------|------|------|
| 长轮询 | iLink getupdates API | `wechat.py:_poll_loop` |
| QR 码登录 | 扫码绑定 bot_token | `wechat.py:_bind_via_qrcode` |
| QR 状态轮询 | 轮询确认/过期/取消状态 | `wechat.py:_bind_via_qrcode` L676-713 |
| AES-128-ECB 加密 | 媒体文件加密传输 | `wechat.py:_encrypt_aes_128_ecb` |
| AES-128-ECB 解密 | 入站媒体解密 | `wechat.py:_decrypt_aes_128_ecb` |
| CDN 下载 | 图片/文件从 CDN 下载并解密 | `wechat.py:_download_cdn_bytes` |
| CDN 上传 | 加密后上传到 CDN | `wechat.py:_upload_cdn_bytes` |
| 图片类型检测 | 魔数检测 PNG/JPEG/GIF/WebP/BMP | `wechat.py:_detect_image_extension_and_mime` |
| 文件类型过滤 | 扩展名 + MIME 类型白名单 | `wechat.py:_is_allowed_file_type` |
| 大小限制 | 图片 20MB / 文件 50MB (可配置) | `wechat.py:DEFAULT_MAX_*` |
| AES 密钥解析 | 支持 hex/base64/urlsafe 多种编码 | `wechat.py:_resolve_media_aes_key` |
| Context Token | 出站消息必须携带 context_token | `wechat.py:_resolve_context_token` |
| Token 过期检测 | errcode=-14 自动停止 | `wechat.py:_poll_loop` L564-571 |
| 游标持久化 | get_updates_buf 持久化到 JSON | `wechat.py:_save_state` |
| 认证状态持久化 | QR 状态/bot_token 持久化到 JSON | `wechat.py:_save_auth_state` |
| 用户过滤 | allowed_users 白名单 | `wechat.py:_check_user` |
| 服务端超时适配 | 自动适配服务器返回的 longpolling_timeout_ms | `wechat.py:_update_longpoll_timeout` |
| 重试机制 | 指数退避 最多 3 次 | `wechat.py:_send_text_message` L343-362 |

## 共享能力

### 命令系统

| 能力 | 描述 | 来源 |
|------|------|------|
| 命令定义 | 统一的命令 frozenset | `commands.py:KNOWN_CHANNEL_COMMANDS` |
| 命令分发 | 各频道解析器检测已知命令 | `feishu.py:_is_feishu_command` 等 |
| bootstrap | 转为 CHAT 消息 + is_bootstrap 上下文 | `manager.py:_handle_command` L951-957 |
| new | 调用 Gateway API 创建新线程 | `manager.py:_handle_command` L959-971 |
| status | 查询当前线程 ID | `manager.py:_handle_command` L972-974 |
| models | HTTP GET Gateway /api/models | `manager.py:_fetch_gateway` |
| memory | HTTP GET Gateway /api/memory | `manager.py:_fetch_gateway` |
| help | 返回硬编码帮助文本 | `manager.py:_handle_command` L979-988 |

### 文件处理

| 能力 | 描述 | 来源 |
|------|------|------|
| 入站文件读取器注册 | `register_inbound_file_reader()` | `manager.py:INBOUND_FILE_READERS` |
| 通用 HTTP 下载 | `_read_http_inbound_file` | `manager.py:_read_http_inbound_file` |
| 企业微信解密下载 | `_read_wecom_inbound_file` (含 AES 解密) | `manager.py:_read_wecom_inbound_file` |
| 微信本地路径读取 | `_read_wechat_inbound_file` (优先读本地) | `manager.py:_read_wechat_inbound_file` |
| 入站文件写入 | 下载后写入沙箱 uploads 目录 | `manager.py:_ingest_inbound_files` |
| 文件命名安全 | normalize_filename + claim_unique_filename | `manager.py:_ingest_inbound_files` L483-490 |
| 上传文件块格式化 | 将文件信息格式化为 LLM 可理解的 XML 块 | `manager.py:_format_uploaded_files_block` |

### 配置与状态

| 能力 | 描述 | 来源 |
|------|------|------|
| 频道注册表 | 名称 → 导入路径的映射 | `service.py:_CHANNEL_REGISTRY` |
| 凭据检测 | 检测各频道的必要凭据字段 | `service.py:_CHANNEL_CREDENTIAL_KEYS` |
| 环境变量覆盖 | DEER_FLOW_CHANNELS_LANGGRAPH_URL 等 | `service.py:_CHANNELS_*_ENV` |
| 状态查询 API | GET /api/channels/ | `gateway/routers/channels.py` |
| 频道重启 API | POST /api/channels/{name}/restart | `gateway/routers/channels.py` |
| 反射导入 | resolve_class 延迟导入适配器 | `service.py:_start_channel` L162-164 |
| 单例模式 | _channel_service 全局单例 | `service.py:_channel_service` |

## 外部依赖

| 依赖 | 用途 | 必需 |
|------|------|------|
| `langgraph-sdk` | 与 Gateway API 通信 | ✅ |
| `httpx` | HTTP 客户端 (CDN/API 调用) | ✅ |
| `lark-oapi` | 飞书 SDK | 飞书频道 |
| `dingtalk-stream` | 钉钉 SDK | 钉钉频道 |
| `wecom-aibot-python-sdk` | 企业微信 SDK | 企业微信频道 |
| `slack-sdk` | Slack SDK | Slack 频道 |
| `discord.py` | Discord SDK | Discord 频道 |
| `python-telegram-bot` | Telegram SDK | Telegram 频道 |
| `cryptography` | AES 加密 (微信媒体) | 微信频道 |
| `markdown_to_mrkdwn` | Markdown→Slack mrkdwn 转换 | Slack 频道 |
