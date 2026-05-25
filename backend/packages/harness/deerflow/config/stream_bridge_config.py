"""Stream Bridge 配置 — Agent 工作器到 SSE 端点的桥接。

Stream Bridge 连接后台 Agent 执行线程和前端的 SSE（Server-Sent Events）流。
当 Agent 在后台线程运行时，中间结果通过 Bridge 传递到 Gateway 的 SSE 端点。

### 后端类型
- memory: 进程内 asyncio.Queue。仅限单进程部署（Gateway 和 Agent 在同一进程）。
- redis: Redis Streams。计划用于 Phase 2 多进程部署（尚未实现）。

### 配置场景
- 本地开发（make dev）: memory 模式足够
- Docker 单容器: memory 模式足够
- 多进程/多节点: 需要 redis 模式（未来实现）
"""

from typing import Literal

from pydantic import BaseModel, Field

StreamBridgeType = Literal["memory", "redis"]


class StreamBridgeConfig(BaseModel):
    """Stream Bridge 配置。

    - type: 后端类型（memory 或 redis）
    - redis_url: Redis URL（仅 redis 类型）
    - queue_maxsize: 每个运行在 memory 模式中的最大缓冲事件数
    """

    type: StreamBridgeType = Field(
        default="memory",
        description="Stream bridge backend type. 'memory' uses in-process asyncio.Queue (single-process only). 'redis' uses Redis Streams (planned for Phase 2, not yet implemented).",
    )
    redis_url: str | None = Field(
        default=None,
        description="Redis URL for the redis stream bridge type. Example: 'redis://localhost:6379/0'.",
    )
    queue_maxsize: int = Field(
        default=256,
        description="Maximum number of events buffered per run in the memory bridge.",
    )


# 全局单例 — None 表示未配置，回退到 memory 默认值
_stream_bridge_config: StreamBridgeConfig | None = None


def get_stream_bridge_config() -> StreamBridgeConfig | None:
    """获取当前 Stream Bridge 配置。"""
    return _stream_bridge_config


def set_stream_bridge_config(config: StreamBridgeConfig | None) -> None:
    """设置 Stream Bridge 配置。"""
    global _stream_bridge_config
    _stream_bridge_config = config


def load_stream_bridge_config_from_dict(config_dict: dict | None) -> None:
    """从字典加载 Stream Bridge 配置（由 AppConfig 初始化时调用）。"""
    global _stream_bridge_config
    if config_dict is None:
        _stream_bridge_config = None
        return
    _stream_bridge_config = StreamBridgeConfig(**config_dict)
