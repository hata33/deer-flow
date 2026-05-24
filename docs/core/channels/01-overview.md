# Channels 系统全局概览

> **定位**: Channels 是 DeerFlow 的多平台 IM 接入层，将外部即时通讯平台
> （飞书、钉钉、企业微信、Slack、Discord、Telegram、微信 iLink）连接到
> DeerFlow Agent，使用户可以通过日常聊天工具与 AI 助手交互。

## 系统定位

Channels 模块解决了 "如何让 Agent 出现在用户日常使用的 IM 工具中" 的问题。
它作为 Gateway 的伴生服务运行，在 Gateway 启动时自动加载。

```
┌──────────────────────────────────────────────────────────────────────┐
│                         Channels 系统边界                              │
│                                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐            │
│  │   飞书    │  │   钉钉    │  │   Slack  │  │   ...    │  外部 IM   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘            │
│       │              │              │              │                  │
│  ┌────┴──────────────┴──────────────┴──────────────┴────┐           │
│  │                   MessageBus                          │  消息中枢  │
│  │              (异步发布/订阅队列)                        │           │
│  └────────────────────────┬─────────────────────────────┘           │
│                           │                                          │
│  ┌────────────────────────┴─────────────────────────────┐           │
│  │                 ChannelManager                        │  核心调度  │
│  │           (langgraph-sdk → Gateway API)              │           │
│  └────────────────────────┬─────────────────────────────┘           │
│                           │                                          │
│  ┌────────────────────────┴─────────────────────────────┐           │
│  │                   Gateway API                         │  后端服务  │
│  │        (LangGraph 兼容 API + REST 端点)               │           │
│  └──────────────────────────────────────────────────────┘           │
└──────────────────────────────────────────────────────────────────────┘
```

## 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| **Channel** | `base.py` | 频道抽象基类，定义 `start`/`stop`/`send`/`send_file` 接口 |
| **MessageBus** | `message_bus.py` | 异步发布/订阅中心，解耦频道与调度器 |
| **ChannelManager** | `manager.py` | 核心调度器，通过 langgraph-sdk 调用 Agent |
| **ChannelService** | `service.py` | 生命周期管理器，从 config.yaml 启动频道 |
| **ChannelStore** | `store.py` | JSON 文件持久化 IM 会话 ↔ DeerFlow 线程映射 |
| **commands** | `commands.py` | 跨频道共享命令定义 |

## 支持的平台

| 平台 | 连接方式 | 流式输出 | 文件支持 | SDK |
|------|---------|---------|---------|-----|
| **飞书/Lark** | WebSocket | ✅ 卡片实时更新 | 图片/文件 | `lark-oapi` |
| **钉钉** | WebSocket (Stream Push) | ✅ AI Card 流式 | 图片/文件 | `dingtalk-stream` |
| **企业微信** | WebSocket | ✅ 流式回复 | 图片/文件(分块上传) | `wecom-aibot-python-sdk` |
| **Slack** | Socket Mode | ❌ | 文件 | `slack-sdk` |
| **Discord** | WebSocket | ❌ | 文件 | `discord.py` |
| **Telegram** | 长轮询 | ❌ | 图片/文件 | `python-telegram-bot` |
| **微信 iLink** | 长轮询 | ❌ | 图片/文件(AES加密) | 无(直接 HTTP) |

## 消息流向

```
入站路径:
  用户发送消息 → IM 平台 → Channel._on_xxx_handler()
    → _make_inbound() → bus.publish_inbound()
    → ChannelManager._dispatch_loop() → _handle_chat()
    → langgraph-sdk client.runs.wait/stream()
    → DeerFlow Agent 处理

出站路径:
  DeerFlow Agent 响应 → ChannelManager
    → bus.publish_outbound(outbound_msg)
    → Channel._on_outbound() → Channel.send()
    → IM 平台 → 用户收到回复
```

## 配置入口

在 `config.yaml` 的 `channels` 段配置：

```yaml
channels:
  langgraph_url: http://localhost:8001/api   # Agent API 地址
  gateway_url: http://localhost:8001          # 辅助查询 API 地址
  session:                                     # 全局默认会话配置
    assistant_id: lead_agent
    config:
      recursion_limit: 100
    context:
      thinking_enabled: true

  feishu:
    enabled: true
    app_id: $FEISHU_APP_ID
    app_secret: $FEISHU_APP_SECRET
```

## 关键设计决策

1. **无公网 IP 要求**: 所有频道使用 WebSocket 或长轮询的出站连接，不需要
   暴露公网端口
2. **多线程架构**: 频道 SDK 客户端在独立线程中运行各自的事件循环，通过
   `run_coroutine_threadsafe` 与主线程通信
3. **松耦合**: 频道与调度器通过 MessageBus 解耦，新增平台只需实现 Channel
   接口即可
4. **线程复用**: 同一 IM 会话的多条消息复用同一个 DeerFlow 线程，通过
   ChannelStore 管理映射关系
5. **优雅降级**: 流式失败时自动回退到非流式，文件上传失败时仅跳过附件
   而不影响文本回复
