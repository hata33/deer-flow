"""沙箱元数据。

支持跨进程发现的沙箱状态持久化数据结构。
不同进程（gateway vs langgraph、多 worker、K8s pod）可通过共享此数据重连已有沙箱。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SandboxInfo:
    """沙箱元数据，用于跨进程发现和状态持久化。

    Attributes:
        sandbox_id: 沙箱唯一标识。
        sandbox_url: 沙箱 API 地址（如 http://localhost:8080）。
        container_name: 容器名称（仅本地容器后端）。
        container_id: 容器 ID（仅本地容器后端）。
        created_at: 创建时间戳。
    """

    sandbox_id: str
    sandbox_url: str  # 如 http://localhost:8080 或 http://k3s:30001
    container_name: str | None = None  # 仅本地容器后端
    container_id: str | None = None  # 仅本地容器后端
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """序列化为字典。"""
        return {
            "sandbox_id": self.sandbox_id,
            "sandbox_url": self.sandbox_url,
            "container_name": self.container_name,
            "container_id": self.container_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SandboxInfo:
        """从字典反序列化（兼容旧字段名 base_url）。"""
        return cls(
            sandbox_id=data["sandbox_id"],
            sandbox_url=data.get("sandbox_url", data.get("base_url", "")),
            container_name=data.get("container_name"),
            container_id=data.get("container_id"),
            created_at=data.get("created_at", time.time()),
        )
