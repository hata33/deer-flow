# Channels 实现分析

> 本文档基于源码逐层拆解实现细节。回答"代码怎么写的、为什么这么写"。

## 模块依赖图

```
service.py (生命周期管理 + 频道注册表)
 ├── MessageBus (异步队列 + 回调广播)
 ├── ChannelStore (JSON 文件线程映射)
 └── ChannelManager (核心调度器)
      ├── langgraph-sdk get_client()  ←── internal_auth.py (进程内 Token)
      ├── _dispatch_loop()           ←── MessageBus.get_inbound()
      ├── _handle_chat()             ←── runs.wait() / runs.stream()
      └── _resolve_run_params()      ←── 多层会话配置合并
           ├── feishu.py (飞书 — WebSocket + Card Patch)
           ├── slack.py (Slack — Socket Mode + mrkdwn)
           ├── dingtalk.py (钉钉 — Stream Push + AI Card)
           └── base.py (Channel 抽象基类)
```

---

## 第 1 层: MessageBus — 异步消息中枢

**InboundMessage** 从 IM 平台进入，核心路由字段：`channel_name`（频道标识）、`chat_id`（平台会话 ID）、`topic_id`（话题标识，同一 chat+topic 复用线程）、`user_id`（平台用户）、`files`（附件元数据）。

**OutboundMessage** 从调度器返回，关键标志位：`is_final`（最终 vs 流式中间）、`attachments`（已解析的 `ResolvedAttachment` 列表）。

```
入站:  Channel._on_message() → bus.publish_inbound() → asyncio.Queue
出站:  bus.publish_outbound() → 遍历回调 → Channel._on_outbound()
         └── 过滤 channel_name → send() + send_file()
```

`_on_outbound()` 先按 `channel_name` 过滤，调用 `send()` 发送文本后遍历 `attachments` 调用 `send_file()`。文本发送失败时跳过文件上传，避免部分投递。

---

## 第 2 层: ChannelManager — 核心调度器

### _dispatch_loop() — 消费循环

超时 1 秒轮询 `bus.get_inbound()`，允许 `_running=False` 时快速退出。每条消息在独立 `asyncio.Task` 中处理，`Semaphore(5)` 限制并发。根据 `msg_type` 分流：`CHAT` 走 Agent 调用，`COMMAND` 走本地命令处理。

### _resolve_run_params() — 多层会话配置

配置合并优先级（从低到高）：代码默认值 → `channels.session` → `channels.<name>.session` → `users.<user_id>` 覆盖。自定义 Agent 通过 `agent_name` 键路由：非 `"lead_agent"` 的 `assistant_id` 归一化后注入 `run_context["agent_name"]`，`make_lead_agent` 读取该键加载对应 `SOUL.md`。

---

## 第 3 层: 线程映射

ChannelStore 键结构：`channel:chat_id`（私聊）或 `channel:chat_id:topic_id`（群聊话题）。映射流程：`store.get_thread_id()` 命中则复用线程，未命中则 `client.threads.create()` 创建新线程并写入 JSON 文件。`/new` 命令覆盖映射实现"重新开始"。

---

## 第 4 层: 平台特定流式实现

### 飞书 — Card Patch 模式

```
收到消息 → _add_reaction("OK") → _reply_card("Working on it...")
  → 追踪 card_id → _running_card_ids[source_id] = card_id
流式更新 → _update_card(card_id, text)  → PatchMessage API 原地更新
  → 最小间隔 0.35s → _build_card_content() 设置 update_multi=true
最终回复 → _update_card(card_id, final) → _add_reaction("DONE") → 清理
```

降级策略：PatchMessage 失败时，最终回复回退到 `_reply_card()`。线程模型：lark-oapi SDK 在导入时捕获事件循环，uvicorn 的 uvloop 与之冲突，需在独立线程中创建新循环并 patch SDK 模块级引用。

### Slack — Wait + mrkdwn 模式

不支持原地更新，使用 `runs.wait()` 阻塞等待完成。收到消息后添加 eyes 反应并发送 "Working on it..." 提示。回复时通过 `markdown_to_mrkdwn` 转换格式，发送后添加 white_check_mark 反应。路由规则：`topic_id = thread_ts`，非线程消息使用 `msg.ts`。

### 钉钉 — AI Card 模式

由 `card_template_id` 配置决定。AI Card 模式：`AICardReplier.async_create_and_deliver_card()` 创建卡片 → `async_streaming()` 流式更新。普通模式使用 `runs.wait()` + `sampleMarkdown`。Markdown 适配：代码块→引用块、内联代码→粗体、表格→键值对列表。路由规则：P2P `topic_id=None`，群聊 `topic_id=msg_id`。

---

## 第 5 层: 文件处理

**入站文件**: 平台元数据 → `INBOUND_FILE_READERS[channel]` 下载 → `claim_unique_filename()` 安全命名 → `write_upload_file_no_symlink()` 写入 → 格式化为 `<uploaded_files>` XML 块注入消息文本。注册表支持平台特定下载（企业微信 AES 解密、微信本地路径读取）。

**出站文件**: artifacts 列表 → 仅接受 `/mnt/user-data/outputs/` 前缀 → `resolve_virtual_path()` 映射 → `relative_to(outputs_dir)` 防路径遍历 → `is_file()` 检查 → 构建 `ResolvedAttachment`。出站文件只在 `is_final=True` 时附带。
