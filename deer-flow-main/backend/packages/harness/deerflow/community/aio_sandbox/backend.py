"""沙箱后端抽象基类。

定义沙箱生命周期的核心接口：创建、销毁、存活检查、发现。
两种实现：LocalContainerBackend（本地 Docker）和 RemoteSandboxBackend（远程 K8s）。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

import requests

from .sandbox_info import SandboxInfo

logger = logging.getLogger(__name__)


def wait_for_sandbox_ready(sandbox_url: str, timeout: int = 30) -> bool:
    """轮询沙箱健康检查端点，等待就绪或超时。

    Args:
        sandbox_url: 沙箱 URL（如 http://k3s:30001）。
        timeout: 最大等待秒数。
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(f"{sandbox_url}/v1/sandbox", timeout=5)
            if response.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    return False


class SandboxBackend(ABC):
    """沙箱后端抽象基类。

    两种实现：
    - LocalContainerBackend：本地启动 Docker/Apple Container，管理端口
    - RemoteSandboxBackend：连接预存的 URL（K8s 服务、外部地址）
    """

    @abstractmethod
    def create(self, thread_id: str, sandbox_id: str, extra_mounts: list[tuple[str, str, bool]] | None = None) -> SandboxInfo:
        """创建/分配沙箱实例。"""
        ...

    @abstractmethod
    def destroy(self, info: SandboxInfo) -> None:
        """销毁沙箱并释放资源。"""
        ...

    @abstractmethod
    def is_alive(self, info: SandboxInfo) -> bool:
        """轻量检查沙箱是否存活（如容器 inspect，不做完整健康检查）。"""
        ...

    @abstractmethod
    def discover(self, sandbox_id: str) -> SandboxInfo | None:
        """通过确定性 ID 发现已有沙箱（用于跨进程恢复）。"""
        ...
