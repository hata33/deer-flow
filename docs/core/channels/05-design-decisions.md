# Channels 设计决策

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

## 核心决策清单

| # | 决策 | 解决的问题 | 权衡 |
|---|------|-----------|------|
| C1 | Pub/Sub MessageBus 解耦入站/出站 | 频道与调度器直接耦合 | 引入异步队列，消息顺序受全局队列影响 |
| C2 | JSON 文件存储线程映射 | 简单持久化、零依赖 | 高并发写入瓶颈、不适合大规模部署 |
| C3 | 按平台差异化流式策略 | 平台能力不同（飞书可更新、Slack 只能等） | 代码路径分叉、平台特定逻辑散布 |
| C4 | langgraph-sdk HTTP 客户端 | 与前端一致的线程管理接口 | 多一跳 HTTP 开销、依赖 SDK 版本兼容 |
| C5 | 进程内内部认证注入 | 频道 Worker 无法持有浏览器 Cookie | 令牌仅进程内有效、不支持跨进程 Worker |

---

## C1: Pub/Sub MessageBus 解耦入站/出站

**动机**: 频道适配器（飞书、Slack、Telegram、钉钉）和 Agent 调度器（`ChannelManager`）是完全不同的关注域。频道负责平台协议（WebSocket 长连接、消息格式解析），调度器负责 Agent 调用和线程管理。直接耦合会导致每个频道都需要知道如何调用 Agent，每次 Agent API 变更都需要修改所有频道。

**解决的问题**:
- 频道适配器与 Agent 调度器之间的紧耦合
- 新增频道需要理解 Agent 调用逻辑（反之亦然）
- 调试困难：消息流向隐藏在直接调用链中

**权衡**:
- **异步队列**: `asyncio.Queue` 是无界队列，极端情况下可能积压大量未处理消息。`ChannelManager` 使用 `Semaphore(max_concurrency=5)` 限制并发，但队列本身不做背压
- **消息顺序**: 所有频道的入站消息共享同一队列。如果飞书消息 A 先于 Slack 消息 B 入队，但 B 的信号量先获得，则 B 可能先被处理
- **可接受的原因**: IM 消息本身就是异步的，用户不期望严格的跨平台消息顺序

**关键实现**: `MessageBus` 使用两个数据结构——入站用 `asyncio.Queue`（FIFO），出站用回调列表（广播）。频道通过 `subscribe_outbound()` 注册回调，`publish_outbound()` 遍历所有回调并调用。每个频道在 `_on_outbound()` 中过滤 `msg.channel_name == self.name`。

---

## C2: JSON 文件存储线程映射

**动机**: 频道系统需要持久化 IM 会话到 DeerFlow 线程的映射（`channel_name:chat_id[:topic_id] -> thread_id`）。对于中小规模部署（几个到几十个并发会话），JSON 文件提供了零依赖、人类可读、调试方便的持久化方案。

**解决的问题**:
- Gateway 重启后 IM 会话需要复用已有的 DeerFlow 线程
- 需要一种简单的方式查看当前活跃的 IM-线程映射

**权衡**:
- **原子性**: 使用临时文件 + `rename` 实现原子写入，避免写入中途崩溃导致数据丢失
- **线程安全**: `threading.Lock` 保护写入操作（而非 `asyncio.Lock`），因为 `_save()` 是同步 I/O
- **扩展性上限**: 每次 `set_thread_id()` 都重写整个 JSON 文件。当映射条目达到数千条时，写入延迟可能变得显著。高并发场景应替换为数据库后端（接口已经抽象为 `ChannelStore` 类，替换实现只需修改此类）
- **可接受的原因**: IM 频道的典型使用规模（个人/团队级）远未触及 JSON 文件的性能上限

**关键实现**: `ChannelStore._key()` 生成两级键——`channel:chat_id`（私聊/根会话）和 `channel:chat_id:topic_id`（群聊话题/线程）。`remove()` 支持前缀删除——不传 `topic_id` 时删除该 `chat_id` 下的所有映射。

---

## C3: 按平台差异化流式策略

**动机**: 不同 IM 平台的消息更新能力差异巨大：

- **飞书**: 支持交互式卡片（Interactive Card），可通过 `PatchMessage` API 原地更新卡片内容，实现打字机效果
- **企业微信**: 与飞书类似的卡片更新能力
- **钉钉**: 支持 AI Card 流式输出（需配置 `card_template_id`），但功能较新且依赖特定 SDK 版本
- **Slack/Telegram/Discord/微信**: 没有原地更新消息的 API，只能发新消息或等待完成后一次性回复

**解决的问题**: 统一流式策略会导致飞书体验降级（等待完成后才回复）或 Slack 体验异常（尝试更新不存在的消息）。

**权衡**:
- **代码分叉**: `ChannelManager._handle_chat()` 根据 `supports_streaming` 分流到 `_handle_streaming_chat()` 或 `runs.wait()` 两条路径
- **飞书卡片追踪**: 飞书频道需要追踪每条消息对应的运行卡片 `message_id`（`_running_card_ids`），以支持原地更新。这增加了内存管理复杂度
- **钉钉 AI Card 降级**: 钉钉 AI Card 创建失败时回退到 `sampleMarkdown` 文本消息，但回退后无法再做流式更新

**关键实现**: `CHANNEL_CAPABILITIES` 字典声明各平台的流式支持。`_channel_supports_streaming()` 优先检查频道实例的 `supports_streaming` 属性（如钉钉的 AI Card 模式由配置决定），回退到字典默认值。

---

## C4: langgraph-sdk HTTP 客户端

**动机**: 频道系统通过 `langgraph-sdk` 的 Python 异步客户端（`get_client()`）与 Gateway 通信。这与前端使用 `useStream` Hook 的通信路径完全一致——都是通过 HTTP 调用 Gateway 的 LangGraph 兼容 API。

**解决的问题**:
- 如果频道直接调用 `RunManager` 等内部对象，会绕过认证、CSRF、授权等安全层
- 使用 HTTP 客户端确保所有请求经过完整的安全中间件栈
- 与前端使用相同的 API，减少接口不一致的风险

**权衡**:
- **性能开销**: 同进程内的 HTTP 调用增加了一跳网络栈（TCP loopback + HTTP 解析）。对于大规模场景，这比直接函数调用慢约 1-2ms
- **SDK 版本兼容**: `langgraph-sdk` 的 API 变更可能同时影响前端和频道系统
- **可接受的原因**: IM 消息的延迟主要来自 Agent 推理（秒级），1-2ms 的 HTTP 开销可忽略不计

**关键实现**: `ChannelManager._get_client()` 延迟初始化 `langgraph-sdk` 异步客户端，注入内部认证头（`X-DeerFlow-Internal-Token`）和 CSRF Token 对（Cookie + Header），确保 Gateway 的安全中间件放行。

---

## C5: 进程内内部认证注入

**动机**: IM 频道 Worker 运行在 Gateway 进程内（由 `lifespan` 的 `start_channel_service()` 启动）。这些 Worker 需要调用 Gateway 的 HTTP API 创建线程、运行 Agent，但它们不是浏览器——无法持有浏览器 Cookie 会话。

**解决的问题**:
- Worker 无法通过标准 Cookie-JWT 认证链路访问 Gateway API
- 如果使用管理员 JWT Token，Token 泄露会影响所有用户
- 如果跳过认证，Gateway 的安全中间件会拒绝请求

**权衡**:
- **令牌作用域**: 进程启动时生成的随机令牌仅在当前进程生命周期内有效（进程重启自动更换）。无法跨进程使用——如果频道 Worker 运行在独立进程中，此机制不适用
- **用户归属**: 内部用户使用 `DEFAULT_USER_ID`（常量 `"default"`）和 `system_role="internal"`。所有通过频道创建的线程归属于此内部用户，而非具体的 IM 用户。这是当前限制——未来可考虑将 IM 用户 ID 映射到 Gateway 用户
- **可接受的原因**: 频道与 Gateway 共进程是当前部署架构的约束（`make dev` 和 Docker 都运行在同一进程中）

**关键实现**: `internal_auth.py` 在模块加载时生成 `_INTERNAL_AUTH_TOKEN`（`secrets.token_urlsafe(32)`）。`create_internal_auth_headers()` 返回包含该 Token 的请求头字典。`AuthMiddleware` 识别有效内部令牌后创建合成用户对象并跳过 JWT 校验。

`ChannelManager._get_client()` 将内部 Token 注入到 `langgraph-sdk` 客户端的默认请求头中，同时注入 CSRF Token 对（`X-CSRF-Token` Header + `csrf_token` Cookie），满足 `CSRFMiddleware` 的 Double Submit 检查。
