# Channels 其他特性与策略

> 描述 Channels 系统中不属于核心生命周期和基础能力的特性、策略和设计
> 考量，包括安全策略、流式策略、线程模型、错误处理策略等。

## 安全策略

### 1. Artifact 路径安全

Agent 产出的文件通过 `present_files` 工具暴露给用户。Channels 在转发
这些文件到 IM 平台前进行严格的安全校验：

```
_resolve_attachments(thread_id, artifacts)
    │
    ├─ 1. 前缀检查: 仅接受 /mnt/user-data/outputs/ 开头的路径
    │      拒绝其他虚拟路径 (uploads, workspace) — 防止数据泄露
    │
    ├─ 2. 路径解析: resolve_virtual_path → 实际文件系统路径
    │
    ├─ 3. 路径遍历防护: 验证解析路径在 outputs_dir 下
    │      actual.resolve().relative_to(outputs_dir)
    │
    └─ 4. 文件存在性: actual.is_file() 确认文件确实存在
```

**策略理由**: 仅允许 outputs 目录下的文件通过 IM 频道传出。uploads
（用户上传的文件）和 workspace（工作目录文件）不应直接暴露给 IM 用户。

### 2. 入站文件名安全

用户通过 IM 上传的文件，在写入沙箱前经过安全处理：

```python
safe_name = claim_unique_filename(normalize_filename(filename), seen_names)
```

- `normalize_filename`: 去除路径分隔符等危险字符
- `claim_unique_filename`: 防止文件名冲突
- `UnsafeUploadPathError`: 捕获非法路径尝试

### 3. CSRF 保护

ChannelManager 调用 Gateway API 时携带 CSRF token：

```python
headers = {
    **create_internal_auth_headers(),
    CSRF_HEADER_NAME: self._csrf_token,
    "Cookie": f"{CSRF_COOKIE_NAME}={self._csrf_token}",
}
```

### 4. 用户过滤

所有平台支持 `allowed_users` 白名单机制：

| 平台 | 过滤字段 | 空列表行为 |
|------|---------|-----------|
| 钉钉 | sender_staff_id | 允许所有 |
| Slack | user_id | 允许所有 |
| Telegram | user_id (int) | 允许所有 |
| 微信 | from_user_id | 允许所有 |

### 5. 微信媒体加密

微信 iLink 的媒体文件使用 AES-128-ECB + PKCS7 填充加密传输：

- **密钥长度**: 128 位 (16 字节)
- **模式**: ECB（电子密码本模式）
- **填充**: PKCS7
- **密钥编码**: 支持 hex、base64、urlsafe_base64 多种格式自动检测

### 6. Discord Guild/Channel 过滤

Discord 额外支持：
- `allowed_guilds`: 限制可交互的服务器
- `allowed_channels`: 在 mention_only 模式下豁免 @提及检查的频道

## 流式策略

### 流式能力矩阵

```
支持流式的频道:
  ┌──────────┐     ┌──────────┐     ┌──────────┐
  │   飞书    │     │ 企业微信  │     │   钉钉   │
  │ PatchMsg │     │ reply_   │     │ AI Card  │
  │ Card更新  │     │ stream   │     │ Stream   │
  └──────────┘     └──────────┘     └──────────┘

不支持流式的频道:
  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
  │  Slack   │  │ Discord  │  │ Telegram │  │  微信    │
  │ runs.wait│  │ runs.wait│  │ runs.wait│  │ runs.wait│
  └──────────┘  └──────────┘  └──────────┘  └──────────┘
```

### 流式判定逻辑

```python
def _channel_supports_streaming(channel_name):
    # 1. 优先检查运行中的频道实例
    service = get_channel_service()
    if service:
        channel = service.get_channel(channel_name)
        if channel:
            return channel.supports_streaming  # 动态属性
    # 2. 回退到静态能力表
    return CHANNEL_CAPABILITIES.get(channel_name, {}).get("supports_streaming", False)
```

### 流式节流策略

为防止消息刷屏，流式输出有最小发布间隔：

```python
STREAM_UPDATE_MIN_INTERVAL_SECONDS = 0.35  # 350ms
```

当 `last_published_text` 相同或距上次发布不足 350ms 时跳过。

### 流式失败回退

| 场景 | 处理 |
|------|------|
| AI Card 创建失败 | 回退到 sampleMarkdown 文本消息 |
| Card 更新失败 (非最终) | 静默跳过该增量 |
| Card 更新失败 (最终) | 回退到新回复 (reply_card) |
| 流式整体失败 | finally 块发送最终消息 (含错误信息) |
| 线程忙碌 | 发送 "会话正在处理其他请求" |

## 多线程模型

### 线程架构

```
┌──────────────────────────────────────────────────────────────┐
│                       Main Thread (uvicorn)                    │
│  ┌──────────────────────────────────────────────────────────┐│
│  │  ChannelManager._dispatch_loop()                          ││
│  │  Channel._on_outbound() → Channel.send()                  ││
│  │  (async, runs on main event loop)                         ││
│  └──────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
           │ run_coroutine_threadsafe()         ▲
           │                                   │
┌──────────┴──────┐  ┌──────────┐  ┌──────────┴──────┐
│  Feishu Thread  │  │  Discord  │  │  Telegram Thread│
│  (独立事件循环)  │  │  Thread   │  │  (独立事件循环)  │
│                 │  │           │  │                  │
│  _on_message()  │  │ on_message│  │  _on_text()     │
│     │            │  │    │      │     │              │
│     ▼            │  │    ▼      │     ▼              │
│  bus.publish_   │  │ bus.pub  │  │ bus.publish_    │
│  inbound()      │  │ inbound()│  │ inbound()       │
└─────────────────┘  └──────────┘  └─────────────────┘
```

### 跨线程通信

所有频道 SDK 在独立线程中运行，通过以下方式与主线程通信：

```python
# SDK 线程 → 主线程 (发布入站消息)
future = asyncio.run_coroutine_threadsafe(
    bus.publish_inbound(inbound),
    self._main_loop,
)
future.add_done_callback(error_logger)

# 主线程 → SDK 线程 (发送出站消息)
# Discord 示例
send_future = asyncio.run_coroutine_threadsafe(
    target.send(chunk),
    self._discord_loop,
)
await asyncio.wrap_future(send_future)
```

### 线程安全保护

| 资源 | 保护方式 |
|------|---------|
| ChannelStore._data | `threading.Lock` |
| Discord._active_threads | `threading.Lock` |
| Discord._typing_tasks | 主线程操作 (dict) |
| DingTalk._incoming_messages | `threading.Lock` |
| WeChat._auth_lock | `asyncio.Lock` |
| WeChat 文件写入 | `threading.Lock` (防文件名冲突) |

## 消息路由策略

### topic_id 路由规则

各平台的消息到 DeerFlow 线程的路由规则不同：

| 平台 | 私聊 | 群聊 (新消息) | 群聊 (回复) |
|------|------|-------------|------------|
| **飞书** | topic_id = msg_id | topic_id = msg_id | topic_id = root_id |
| **钉钉** | topic_id = None (共享线程) | topic_id = msg_id | N/A |
| **Slack** | thread_ts = 消息 ts | thread_ts = 消息 ts | thread_ts = 父消息 ts |
| **Discord** | topic_id = thread_id | topic_id = channel_id (无 thread_mode) | topic_id = thread_id |
| **Telegram** | topic_id = None (共享线程) | topic_id = msg_id | topic_id = reply_msg_id |
| **企业微信** | topic_id = user_id (共享线程) | N/A | N/A |
| **微信** | topic_id = None (共享线程) | N/A | N/A |

### chat_id vs topic_id

```
Store 键:
  私聊模式:   <channel>:<chat_id>
  群聊+话题:  <channel>:<chat_id>:<topic_id>

示例:
  "telegram:123456789"                     → Telegram 私聊线程
  "feishu:oc_xxx:om_yyy"                  → 飞书群聊中 root_id=om_yyy 的话题
  "discord:789012345:1234567890123456789"  → Discord 线程
  "wecom:user_abc"                        → 企业微信用户线程
```

## 错误处理策略

### 分层异常处理

```
Level 1: Channel 层
  - SDK 连接失败 → 记录错误，频道不启动
  - 消息发送失败 → 重试 (指数退避) → 最终失败时记录日志

Level 2: ChannelManager 层
  - 消息处理异常 → _handle_message catch → _send_error()
  - 配置异常 (InvalidChannelSessionConfigError) → 发送具体错误到用户
  - 线程忙碌 (ConflictError) → "会话正在处理其他请求"

Level 3: ChannelService 层
  - 频道启动失败 → 记录警告，其他频道继续
  - 频道停止异常 → 记录异常，继续关闭其他频道

Level 4: Gateway 层
  - ChannelService 启动失败 → 记录异常，Gateway 继续运行
  - Shutdown hook 超时 → 5 秒超时保护
```

### 重试策略

所有平台的发送操作 (send/send_file) 统一使用指数退避重试：

```python
for attempt in range(_max_retries):  # 默认 3 次
    try:
        await send_operation()
        return  # 成功
    except Exception as exc:
        if attempt < _max_retries - 1:
            delay = 2 ** attempt  # 1s, 2s
            await asyncio.sleep(delay)
raise last_exc  # 全部失败
```

### 元数据瘦身

为避免出站消息元数据过大（如包含原始 raw_message），在构造
OutboundMessage 时自动删除已知的大键：

```python
_METADATA_DROP_KEYS = frozenset({"raw_message", "ref_msg"})

def _slim_metadata(meta):
    return {k: v for k, v in meta.items() if k not in _METADATA_DROP_KEYS}
```

## 配置策略

### 频道启用判断

```python
if not channel_config.get("enabled", False):
    # 频道未启用，检查是否有凭据配置
    has_creds = any(channel_config.get(k) for k in cred_keys)
    if has_creds:
        logger.warning("频道有凭据但未启用")  # 提示用户
    else:
        logger.info("频道未启用，跳过")         # 正常跳过
    continue
```

### 服务 URL 解析优先级

```
langgraph_url 解析:
  1. config.yaml: channels.langgraph_url
  2. 环境变量: DEER_FLOW_CHANNELS_LANGGRAPH_URL
  3. 默认值: http://localhost:8001/api

gateway_url 解析:
  1. config.yaml: channels.gateway_url
  2. 环境变量: DEER_FLOW_CHANNELS_GATEWAY_URL
  3. 默认值: http://localhost:8001
```

### Docker 部署 URL

在 Docker 环境中，应使用容器 DNS 名称：

```yaml
channels:
  langgraph_url: http://gateway:8001/api
  gateway_url: http://gateway:8001
```

## 持久化策略

### 数据存储位置

```
.deer-flow/
├── channels/
│   └── store.json           # ChannelStore: IM→Thread 映射
├── discord_threads.json     # Discord: Channel→Discord Thread 映射
└── wechat/
    ├── state/
    │   └── wechat-getupdates.json  # 微信: 长轮询游标
    ├── wechat-auth.json            # 微信: 认证状态
    └── downloads/                  # 微信: 下载的媒体文件
```

### 原子写入

ChannelStore 使用临时文件 + rename 实现原子写入，防止写入过程中
进程崩溃导致数据损坏：

```python
def _save(self):
    fd = tempfile.NamedTemporaryFile(dir=..., suffix=".tmp", delete=False)
    json.dump(self._data, fd, indent=2)
    fd.close()
    Path(fd.name).replace(self._path)  # 原子替换
```

## 环回警告剥离

Agent 在检测到循环时会在工具调用消息中附加 `[LOOP DETECTED]` 标签。
响应文本提取时自动剥离这些行，避免将内部警告暴露给 IM 用户：

```python
def _strip_loop_warning_text(text):
    if "[LOOP DETECTED]" not in text:
        return text
    return "\n".join(
        line for line in text.splitlines()
        if "[LOOP DETECTED]" not in line
    ).strip()
```

## ChannelService 单例模式

```python
_channel_service: ChannelService | None = None

async def start_channel_service(app_config):
    global _channel_service
    if _channel_service is not None:
        return _channel_service  # 已启动，返回现有实例
    _channel_service = ChannelService.from_app_config(app_config)
    await _channel_service.start()
    return _channel_service
```

通过 Gateway REST API 暴露的状态查询和重启功能都依赖此单例。
