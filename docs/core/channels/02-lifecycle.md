# Channels 完整生命周期

> 详细描述 Channels 系统从启动到运行的完整生命周期，包括 ChannelService、
> ChannelManager 和各频道适配器的初始化、运行和关闭流程。

## 整体生命周期

```
Gateway lifespan 启动
    │
    ├─ 1. 加载 app_config (config.yaml)
    │
    ├─ 2. 启动 LangGraph Runtime (StreamBridge, RunManager, Checkpointer, Store)
    │
    ├─ 3. 检查 Admin 用户 (首次启动引导)
    │
    ├─ 4. start_channel_service(app_config)    ← Channels 入口
    │      │
    │      ├─ 4.1 ChannelService.from_app_config()
    │      │      ├── 创建 MessageBus (asyncio.Queue)
    │      │      ├── 创建 ChannelStore (JSON 文件)
    │      │      ├── 创建 ChannelManager (langgraph-sdk 客户端)
    │      │      └── 解析 URL 配置 (config > env > default)
    │      │
    │      └─ 4.2 ChannelService.start()
    │             ├── 启动 ChannelManager._dispatch_loop()
    │             └── 遍历 channels 配置
    │                  ├── enabled=true  → 导入适配器 → Channel.start()
    │                  └── enabled=false → 跳过
    │
    └─ 5. yield (Gateway 正常运行)
         │
         └─ 6. shutdown: stop_channel_service()
                ├── ChannelService.stop()
                │      ├── 停止各频道 (Channel.stop())
                │      └── 停止 ChannelManager
                └── 清理资源
```

## ChannelService 生命周期

### 1. 构造阶段 (`ChannelService.__init__`)

```python
class ChannelService:
    def __init__(self, channels_config):
        self.bus = MessageBus()                    # 创建消息总线
        self.store = ChannelStore()                # 创建线程映射存储
        self.manager = ChannelManager(             # 创建调度器
            bus=self.bus,
            store=self.store,
            langgraph_url=...,
            gateway_url=...,
            default_session=...,
            channel_sessions=...,
        )
        self._channels = {}                        # name → Channel 实例
```

**关键点**:
- MessageBus、ChannelStore、ChannelManager 在构造时创建
- 各频道适配器延迟到 `start()` 时才导入和实例化
- URL 解析优先级: config.yaml > 环境变量 > 默认值

### 2. 启动阶段 (`ChannelService.start`)

```
ChannelService.start()
    │
    ├─ await self.manager.start()     # 启动调度循环
    │      └─ 创建 asyncio.Task: _dispatch_loop()
    │           └─ 循环: await bus.get_inbound() → _handle_message(msg)
    │
    └─ for name, config in self._config.items():
           ├─ enabled=true → _start_channel(name, config)
           │      │
           │      ├─ 1. resolve_class(import_path)     # 反射导入适配器类
           │      ├─ 2. Channel(bus, config)            # 实例化
           │      ├─ 3. await channel.start()            # 启动平台连接
           │      ├─ 4. 校验 channel.is_running          # 确认运行状态
           │      └─ 5. self._channels[name] = channel   # 注册到字典
           │
           └─ enabled=false → 跳过（有凭据时警告）
```

## Channel 实例生命周期

### 抽象接口

```python
class Channel(ABC):
    def __init__(self, name, bus, config):
        self.name = name           # 频道名称 "feishu"/"slack"/...
        self.bus = bus             # 共享的 MessageBus
        self.config = config       # 平台配置 (app_id, bot_token, ...)
        self._running = False

    @abstractmethod
    async def start(self): ...     # 建立连接，订阅 bus

    @abstractmethod
    async def stop(self): ...      # 断开连接，取消订阅

    @abstractmethod
    async def send(self, msg): ... # 发送出站消息

    async def send_file(self, msg, attachment) -> bool: ...  # 可选的
    async def receive_file(self, msg, thread_id): ...        # 可选的
```

### 典型频道启动流程 (以飞书为例)

```
FeishuChannel.start()
    │
    ├─ 1. 导入 lark-oapi SDK
    │
    ├─ 2. 创建 API Client (app_id + app_secret)
    │
    ├─ 3. self._running = True
    │
    ├─ 4. self.bus.subscribe_outbound(self._on_outbound)
    │      注册出站回调，接收 Agent 回复
    │
    └─ 5. 启动 WebSocket 线程
           │
           ├─ threading.Thread(target=self._run_ws, daemon=True)
           │      │
           │      ├─ 创建新的 asyncio 事件循环
           │      ├─ patch lark-oapi 的模块级 loop 引用
           │      ├─ 注册 IM 消息回调 (self._on_message)
           │      └─ ws_client.start()   ← 阻塞直到断开
           │
           └─ thread.start()
```

### 入站消息处理流程

```
IM 平台推送事件
    │
    ▼
Channel._on_xxx_handler()          (运行在 SDK 线程)
    │
    ├─ 1. 解析消息内容 (text / image / file / mixed)
    ├─ 2. 用户过滤 (allowed_users)
    ├─ 3. 命令检测 (是否以 / 开头且为已知命令)
    ├─ 4. 确定 topic_id 路由规则
    │      ├─ 私聊: topic_id = None
    │      ├─ 群聊+回复: topic_id = root_id
    │      └─ 群聊+新消息: topic_id = msg_id
    │
    ├─ 5. self._make_inbound(chat_id, user_id, text, ...)
    │      └─ 返回 InboundMessage 实例
    │
    └─ 6. run_coroutine_threadsafe(
           bus.publish_inbound(inbound),    ← 跨线程投递到主循环
           main_loop
       )
            │
            ▼
        MessageBus._inbound_queue.put(msg)   (异步队列)
```

## ChannelManager 调度生命周期

### 调度循环

```python
async def _dispatch_loop(self):
    while self._running:
        try:
            msg = await asyncio.wait_for(bus.get_inbound(), timeout=1.0)
        except TimeoutError:
            continue                     # 每 1 秒检查一次 _running

        # 为每条消息创建独立 Task
        task = asyncio.create_task(self._handle_message(msg))
        task.add_done_callback(self._log_task_error)
```

### 消息处理

```python
async def _handle_message(self, msg):
    async with self._semaphore:         # 并发控制 (max 5)
        if msg.msg_type == COMMAND:
            await self._handle_command(msg)
        else:
            await self._handle_chat(msg)
```

### 聊天处理 (非流式)

```
_handle_chat(msg)
    │
    ├─ 1. thread_id = store.get_thread_id(channel, chat_id, topic_id)
    │      ├─ 找到 → 复用已有线程
    │      └─ 未找到 → client.threads.create() → store.set_thread_id()
    │
    ├─ 2. _resolve_run_params(msg, thread_id)
    │      └─ 合并 全局 → channel → user 三层配置
    │
    ├─ 3. 文件预处理
    │      ├─ channel.receive_file(msg, thread_id)    # 频道级文件下载
    │      └─ _ingest_inbound_files(thread_id, msg)   # 入站文件 → uploads
    │
    ├─ 4. client.runs.wait(thread_id, input=...)
    │      │
    │      ├─ 成功 → 提取响应文本 + artifacts
    │      └─ ConflictError → 返回 "会话忙碌" 提示
    │
    ├─ 5. _prepare_artifact_delivery(thread_id, text, artifacts)
    │      └─ 解析 /mnt/user-data/outputs/ 下的文件 → ResolvedAttachment
    │
    └─ 6. bus.publish_outbound(OutboundMessage(...))
           │
           ▼
        Channel._on_outbound(msg)
           │
           ├─ await self.send(msg)          # 发送文本
           └─ for attachment in msg.attachments:
                  await self.send_file(msg, attachment)  # 上传文件
```

### 流式处理

```
_handle_streaming_chat(client, msg, thread_id, ...)
    │
    ├─ 1. async for chunk in client.runs.stream(
    │       thread_id, input=...,
    │       stream_mode=["messages-tuple", "values"]
    │   ):
    │       │
    │       ├─ event="messages-tuple":
    │       │      └─ _accumulate_stream_text() → 增量文本
    │       │
    │       └─ event="values":
    │              └─ _extract_response_text() → 全量快照
    │       │
    │       ├─ 节流: 最小发布间隔 350ms
    │       └─ bus.publish_outbound(OutboundMessage(..., is_final=False))
    │
    └─ 2. finally:
           ├─ 提取最终响应 + artifacts
           └─ bus.publish_outbound(OutboundMessage(..., is_final=True))
```

## 关闭生命周期

```
Gateway lifespan shutdown
    │
    └─ stop_channel_service()
           │
           └─ ChannelService.stop()
                  │
                  ├─ for channel in self._channels:
                  │      await channel.stop()
                  │      │
                  │      ├─ self._running = False
                  │      ├─ bus.unsubscribe_outbound(self._on_outbound)
                  │      ├─ 取消后台任务 (typing, reactions)
                  │      ├─ 断开 WebSocket / 停止 polling
                  │      └─ thread.join(timeout=5)
                  │
                  └─ await self.manager.stop()
                         ├─ self._running = False
                         ├─ task.cancel()
                         └─ await task  (等待当前消息处理完成)
```

## 异常处理与恢复

| 异常场景 | 处理策略 |
|---------|---------|
| SDK 导入失败 | 记录错误日志，跳过该频道，不影响其他频道 |
| WebSocket 断开 | SDK 线程退出，频道 `is_running` 变为 False（可通过 API 重启） |
| 消息处理异常 | 在 `_handle_message` 捕获，发送错误回复到用户 |
| 线程忙碌 (ConflictError) | 返回 "会话正在处理其他请求" 提示 |
| 流式处理异常 | 在 finally 块中发送最终消息（含错误信息） |
| 微信 token 过期 | 检测 errcode=-14，清空 token 并停止频道 |
| 文件上传失败 | 跳过该附件，文本回复正常发送 |
| 配置文件错误 (InvalidChannelSessionConfigError) | 发送具体错误描述到用户 |
