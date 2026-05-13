"""本地沙箱实现。

基于宿主机文件系统的沙箱，通过虚拟路径映射实现隔离。
"""

from .local_sandbox_provider import LocalSandboxProvider

__all__ = ["LocalSandboxProvider"]
