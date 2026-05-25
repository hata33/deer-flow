"""
AIO Sandbox 模块 — 基于 Docker 的沙箱隔离子系统

本模块提供了 DeerFlow 系统中用于代码执行和文件操作的沙箱环境。
沙箱通过 Docker 容器实现进程级隔离，确保用户代码在安全的环境中运行。

架构概述:
    - AioSandbox: 沙箱实例，封装了与容器内 agent-infra/sandbox HTTP API 的交互
    - AioSandboxProvider: 沙箱生命周期管理器，负责创建、复用、销毁沙箱实例
    - SandboxBackend: 抽象后端接口，定义沙箱的创建/销毁/发现等操作
    - LocalContainerBackend: 本地 Docker/Apple Container 后端实现
    - RemoteSandboxBackend: 远程 K8s/Provisioner 后端实现
    - SandboxInfo: 沙箱元数据，支持跨进程发现和状态持久化

设计决策:
    - 使用确定性沙箱 ID（基于 thread_id 的 SHA256 哈希），使多进程间可发现同一沙箱
    - 通过"暖池"（warm pool）机制实现沙箱复用，避免频繁冷启动
    - 支持本地和远程两种部署模式，通过配置切换
    - 内置空闲超时机制，自动回收长时间未使用的沙箱资源

使用方式:
    在 config.yaml 中配置:
        sandbox:
            use: deerflow.community.aio_sandbox:AioSandboxProvider
            image: <容器镜像>
            idle_timeout: 600
            replicas: 3
"""

from .aio_sandbox import AioSandbox
from .aio_sandbox_provider import AioSandboxProvider
from .backend import SandboxBackend
from .local_backend import LocalContainerBackend
from .remote_backend import RemoteSandboxBackend
from .sandbox_info import SandboxInfo

__all__ = [
    "AioSandbox",
    "AioSandboxProvider",
    "LocalContainerBackend",
    "RemoteSandboxBackend",
    "SandboxBackend",
    "SandboxInfo",
]
