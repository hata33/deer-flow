"""流桥接（Stream Bridge）配置。

本模块定义了 DeerFlow 流桥接系统的配置。
流桥接负责将 LangGraph 代理工作进程产生的流式事件转发到 SSE（Server-Sent Events）端点。

工作原理：
    当使用多进程部署（如 Gunicorn 多 worker）时，代理在一个 worker 中运行，
    但 SSE 连接可能在另一个 worker 上。流桥接提供跨进程的事件传递机制。

支持的后端类型：
    - **memory** — 进程内 asyncio.Queue 实现（默认）。
        仅适用于单进程部署，事件在进程内直接传递。
        queue_maxsize 控制每个 run 的最大缓冲事件数。
    - **redis** — Redis Streams 实现（计划中，Phase 2）。
        适用于多进程部署，通过 Redis 在 worker 间传递事件。
        需要 redis_url 配置 Redis 连接地址。

配置示例（config.yaml）：
    ```yaml
    stream_bridge:
      type: memory
      queue_maxsize: 256

    # Redis 模式（未来支持）
    stream_bridge:
      type: redis
      redis_url: redis://localhost:6379/0
    ```

注意：
    - 未配置时（None），系统回退到 memory 类型并使用默认参数。
    - Redis 类型目前尚未实现，仅保留配置接口。
"""
from typing import Literal

from pydantic import BaseModel, Field

# 支持的流桥接后端类型
StreamBridgeType = Literal["memory", "redis"]


class StreamBridgeConfig(BaseModel):
    """流桥接配置。

    Attributes:
        type: 后端类型。
            - 'memory': 进程内 asyncio.Queue（仅单进程）
            - 'redis': Redis Streams（计划中，多进程支持）
        redis_url: Redis 连接 URL（redis 类型时使用）。
            如 ``redis://localhost:6379/0``。
        queue_maxsize: 每个 run 在 memory 桥接中的最大缓冲事件数。
    """

    type: StreamBridgeType = Field(
        default="memory",
        description="流桥接后端类型。'memory' 使用进程内 asyncio.Queue（仅单进程）。'redis' 使用 Redis Streams（计划中的 Phase 2，尚未实现）。",
    )
    redis_url: str | None = Field(
        default=None,
        description="Redis 流桥接类型的连接 URL（如 'redis://localhost:6379/0'）。",
    )
    queue_maxsize: int = Field(
        default=256,
        description="memory 桥接中每个 run 的最大缓冲事件数。",
    )


# ── 全局配置实例 ──────────────────────────────────────────────────────────
# None 表示未配置流桥接（回退到 memory 默认值）
_stream_bridge_config: StreamBridgeConfig | None = None


def get_stream_bridge_config() -> StreamBridgeConfig | None:
    """获取当前流桥接配置。未配置时返回 None。"""
    return _stream_bridge_config


def set_stream_bridge_config(config: StreamBridgeConfig | None) -> None:
    """直接设置流桥接配置。"""
    global _stream_bridge_config
    _stream_bridge_config = config


def load_stream_bridge_config_from_dict(config_dict: dict) -> None:
    """从字典加载流桥接配置（由 AppConfig.from_file 调用）。"""
    global _stream_bridge_config
    _stream_bridge_config = StreamBridgeConfig(**config_dict)
