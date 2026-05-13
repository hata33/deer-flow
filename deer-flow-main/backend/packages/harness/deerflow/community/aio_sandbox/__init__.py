"""AIO 沙箱模块。

基于 Docker/Apple Container 的隔离沙箱实现，支持本地容器和远程 K8s 两种后端。
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
