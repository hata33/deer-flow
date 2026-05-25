"""
沙箱元数据 — 支持跨进程发现和状态持久化

本模块定义了 SandboxInfo 数据类，承载沙箱实例的所有连接信息和元数据。
该数据结构是沙箱系统跨进程协调的基础：

- 跨进程发现: 不同进程（如 gateway 和 langgraph worker）通过共享的
  SandboxInfo 数据重新连接到同一个沙箱容器
- 状态持久化: SandboxInfo 可以序列化为字典格式，用于存储到文件或
  传递给其他服务
- 启动协调: 进程重启后通过 SandboxInfo 列表发现之前遗留的运行中容器

字段说明:
    - sandbox_id: 沙箱的唯一确定性标识符（由 thread_id 的 SHA256 哈希派生）
    - sandbox_url: 沙箱 API 的访问地址（例如 http://localhost:8080）
    - container_name: Docker 容器名称（仅本地容器后端使用）
    - container_id: Docker 容器 ID（仅本地容器后端使用）
    - created_at: 容器创建时间戳，用于计算空闲时长和孤儿检测
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SandboxInfo:
    """持久化的沙箱元数据，支持跨进程发现。

    该数据类包含从不同进程重新连接到现有沙箱所需的所有信息
    （例如 gateway 与 langgraph、多个 worker，或 K8s 中共享存储的
    多个 Pod 之间）。

    Attributes:
        sandbox_id: 沙箱的唯一确定性标识符。
        sandbox_url: 沙箱 API 的访问地址（例如 http://localhost:8080 或 http://k3s:30001）。
        container_name: Docker 容器名称（仅本地容器后端使用）。
        container_id: Docker 容器 ID（仅本地容器后端使用）。
        created_at: 容器创建时间（Unix 纪元时间戳），用于空闲时长计算。
    """

    sandbox_id: str
    sandbox_url: str  # 例如 http://localhost:8080 或 http://k3s:30001
    container_name: str | None = None  # 仅本地容器后端使用
    container_id: str | None = None  # 仅本地容器后端使用
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """将沙箱元数据序列化为字典格式。

        用于状态持久化、跨进程传递或存储到文件。

        Returns:
            包含所有沙箱元数据字段的字典。
        """
        return {
            "sandbox_id": self.sandbox_id,
            "sandbox_url": self.sandbox_url,
            "container_name": self.container_name,
            "container_id": self.container_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SandboxInfo:
        """从字典反序列化沙箱元数据。

        支持从 to_dict() 的输出恢复沙箱元数据，同时也兼容
        base_url 字段名（旧版格式兼容）。

        Args:
            data: 包含沙箱元数据的字典。

        Returns:
            重建的 SandboxInfo 实例。
        """
        return cls(
            sandbox_id=data["sandbox_id"],
            sandbox_url=data.get("sandbox_url", data.get("base_url", "")),
            container_name=data.get("container_name"),
            container_id=data.get("container_id"),
            created_at=data.get("created_at", time.time()),
        )
