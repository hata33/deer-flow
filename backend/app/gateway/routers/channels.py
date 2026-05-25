"""IM（即时通讯）频道管理路由。

本模块提供 IM 频道服务的状态查询和运维管理接口。IM 频道是 DeerFlow
与外部通讯平台（如 Slack、Discord、Telegram 等）集成的桥梁，允许
AI 智能体通过这些平台与用户交互。

当前功能：
- 查询所有频道的运行状态（服务是否运行中、各频道连接状态）
- 重启指定频道（用于故障恢复或配置热更新后生效）

架构说明：
- 频道服务（ChannelService）是全局单例，通过延迟导入获取
- 服务未启动时，状态查询返回 service_running=False
- 重启操作为异步执行，避免阻塞其他请求

路由前缀：/api/channels
标签：channels
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels", tags=["channels"])


class ChannelStatusResponse(BaseModel):
    """频道状态查询响应模型。

    Attributes:
        service_running: 频道服务是否正在运行。
        channels: 各频道的状态信息字典，键为频道名称。
    """

    service_running: bool
    channels: dict[str, dict]


class ChannelRestartResponse(BaseModel):
    """频道重启操作响应模型。

    Attributes:
        success: 重启是否成功。
        message: 结果描述消息。
    """

    success: bool
    message: str


@router.get("/", response_model=ChannelStatusResponse)
async def get_channels_status() -> ChannelStatusResponse:
    """获取所有 IM 频道的运行状态。

    返回频道服务的整体运行状态及各频道的详细信息。
    若频道服务未启动，返回 service_running=False 和空频道列表。

    Returns:
        ChannelStatusResponse，包含服务运行状态和各频道信息。
    """
    from app.channels.service import get_channel_service

    service = get_channel_service()
    if service is None:
        return ChannelStatusResponse(service_running=False, channels={})
    status = service.get_status()
    return ChannelStatusResponse(**status)


@router.post("/{name}/restart", response_model=ChannelRestartResponse)
async def restart_channel(name: str) -> ChannelRestartResponse:
    """重启指定的 IM 频道。

    用于故障恢复或配置热更新后使新配置生效。
    若频道服务未运行，返回 503 Service Unavailable。

    Args:
        name: 频道名称。

    Returns:
        ChannelRestartResponse，包含操作结果和描述消息。

    Raises:
        HTTPException: 状态码 503，当频道服务未运行时抛出。
    """
    from app.channels.service import get_channel_service

    service = get_channel_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Channel service is not running")

    success = await service.restart_channel(name)
    if success:
        logger.info("Channel %s restarted successfully", name)
        return ChannelRestartResponse(success=True, message=f"Channel {name} restarted successfully")
    else:
        logger.warning("Failed to restart channel %s", name)
        return ChannelRestartResponse(success=False, message=f"Failed to restart channel {name}")
