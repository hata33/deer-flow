"""IM 即时通讯频道集成模块。

**模块定位**

本模块是 DeerFlow 的多平台 IM 接入层，提供可插拔的频道系统，
将外部即时通讯平台（飞书/Lark、Slack、Telegram、钉钉、Discord、
企业微信、微信 iLink）连接到 DeerFlow Agent。

**核心架构**

::

    外部 IM 平台 ──→ Channel 实例 ──→ MessageBus ──→ ChannelManager ──→ Gateway API
        (接收消息)      (发布入站)      (异步队列)      (LangGraph SDK)      (Agent 运行时)

- **Channel**: 各平台适配器基类，负责接收消息并发布到 MessageBus
- **MessageBus**: 异步发布/订阅中心，解耦频道与调度器
- **ChannelManager**: 核心调度器，通过 langgraph-sdk 与 Gateway 的
  LangGraph 兼容 API 通信，管理线程映射、流式响应、文件处理等
- **ChannelService**: 生命周期管理器，从 config.yaml 读取配置并启动各频道
- **ChannelStore**: JSON 文件持久化的 IM 会话 → DeerFlow 线程映射存储

**支持的平台**

===========  ========  ============  ============
平台          连接方式   流式支持      文件支持
===========  ========  ============  ============
飞书/Lark     WebSocket 卡片实时更新   图片/文件
钉钉          WebSocket AI Card 流式  图片/文件
企业微信       WebSocket 流式回复      图片/文件(分块上传)
Slack         Socket    无            文件
Discord       WebSocket 无            文件
Telegram      长轮询    无            图片/文件
微信 iLink    长轮询    无            图片/文件(AES加密)
===========  ========  ============  ============
"""

from app.channels.base import Channel
from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage

__all__ = [
    "Channel",
    "InboundMessage",
    "MessageBus",
    "OutboundMessage",
]
