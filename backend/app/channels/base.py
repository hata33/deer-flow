"""IM 频道抽象基类。

定义了所有 IM 频道实现必须遵循的接口规范。每个频道适配器继承
此基类并实现平台特定的消息收发逻辑。

**频道生命周期**

::

    1. 构造 (__init__)      — 注入 MessageBus 和配置
    2. 启动 (start)          — 建立连接，订阅出站回调
    3. 运行中                — 接收消息 → 发布入站；接收出站 → 发送回复
    4. 停止 (stop)           — 断开连接，取消订阅

**消息流向**

::

    入站:  IM 平台 → Channel._on_message_handler → bus.publish_inbound
    出站:  bus.publish_outbound → Channel._on_outbound → Channel.send
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus, OutboundMessage, ResolvedAttachment

logger = logging.getLogger(__name__)


class Channel(ABC):
    """所有 IM 频道实现的抽象基类。

    **子类必须实现的抽象方法**

    - ``start()``: 启动频道，开始监听外部平台的消息
    - ``stop()``: 优雅关闭频道
    - ``send(msg)``: 向外部平台发送消息

    **可选的覆盖方法**

    - ``send_file(msg, attachment)``: 向外部平台上传文件（默认返回 False）
    - ``receive_file(msg, thread_id)``: 下载并处理入站文件（默认透传）

    **频道间通信**

    所有频道通过共享的 MessageBus 实例进行通信：

    - 入站：调用 ``_make_inbound()`` 构造消息 → ``bus.publish_inbound()`` 发布
    - 出站：通过 ``_on_outbound()`` 回调接收消息 → 调用 ``send()`` 发送
    """

    def __init__(self, name: str, bus: MessageBus, config: dict[str, Any]) -> None:
        self.name = name
        self.bus = bus
        self.config = config
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def supports_streaming(self) -> bool:
        return False

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Start listening for messages from the external platform."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the channel."""

    # -- outbound ----------------------------------------------------------

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message back to the external platform.

        The implementation should use ``msg.chat_id`` and ``msg.thread_ts``
        to route the reply to the correct conversation/thread.
        """

    async def send_file(self, msg: OutboundMessage, attachment: ResolvedAttachment) -> bool:
        """Upload a single file attachment to the platform.

        Returns True if the upload succeeded, False otherwise.
        Default implementation returns False (no file upload support).
        """
        return False

    # -- helpers -----------------------------------------------------------

    def _make_inbound(
        self,
        chat_id: str,
        user_id: str,
        text: str,
        *,
        msg_type: InboundMessageType = InboundMessageType.CHAT,
        thread_ts: str | None = None,
        files: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> InboundMessage:
        """Convenience factory for creating InboundMessage instances."""
        return InboundMessage(
            channel_name=self.name,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            msg_type=msg_type,
            thread_ts=thread_ts,
            files=files or [],
            metadata=metadata or {},
        )

    async def _on_outbound(self, msg: OutboundMessage) -> None:
        """Outbound callback registered with the bus.

        Only forwards messages targeted at this channel.
        Sends the text message first, then uploads any file attachments.
        File uploads are skipped entirely when the text send fails to avoid
        partial deliveries (files without accompanying text).
        """
        if msg.channel_name == self.name:
            try:
                await self.send(msg)
            except Exception:
                logger.exception(
                    "Failed to send outbound message on channel %s", self.name)
                return  # Do not attempt file uploads when the text message failed

            for attachment in msg.attachments:
                try:
                    success = await self.send_file(msg, attachment)
                    if not success:
                        logger.warning(
                            "[%s] file upload skipped for %s", self.name, attachment.filename)
                except Exception:
                    logger.exception(
                        "[%s] failed to upload file %s", self.name, attachment.filename)

    async def receive_file(self, msg: InboundMessage, thread_id: str) -> InboundMessage:
        """
        Optionally process and materialize inbound file attachments for this channel.

        By default, this method does nothing and simply returns the original message.
        Subclasses (e.g. FeishuChannel) may override this to download files (images, documents, etc)
        referenced in msg.files, save them to the sandbox, and update msg.text to include
        the sandbox file paths for downstream model consumption.

        Args:
            msg: The inbound message, possibly containing file metadata in msg.files.
            thread_id: The resolved DeerFlow thread ID for sandbox path context.

        Returns:
            The (possibly modified) InboundMessage, with text and/or files updated as needed.
        """
        return msg
