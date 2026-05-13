"""沙箱执行模块。

提供命令执行和文件操作的隔离环境，支持本地沙箱和 Docker 沙箱两种实现。
Agent 通过虚拟路径（/mnt/user-data/）访问文件，沙箱负责路径映射和安全隔离。
"""

from .sandbox import Sandbox
from .sandbox_provider import SandboxProvider, get_sandbox_provider

__all__ = [
    "Sandbox",                  # 沙箱抽象接口
    "SandboxProvider",          # 沙箱提供者协议
    "get_sandbox_provider",     # 获取全局沙箱提供者实例
]
