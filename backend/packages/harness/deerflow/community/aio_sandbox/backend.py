"""
沙箱后端抽象基类 — 定义沙箱配置（provisioning）的统一接口

本模块定义了 SandboxBackend 抽象基类，所有沙箱后端实现都必须遵循此接口。
后端负责沙箱容器的实际创建、销毁、健康检查和发现操作。

两种内置实现:
    - LocalContainerBackend: 本地 Docker/Apple Container 模式
      直接管理本地容器，处理端口分配和容器生命周期
    - RemoteSandboxBackend: 远程 K8s/Provisioner 模式
      通过 HTTP API 将容器管理委托给 Provisioner 服务

接口设计原则:
    - discover() 方法支持跨进程沙箱恢复：利用确定性容器名称，一个进程
      可以发现另一个进程启动的沙箱
    - list_running() 方法支持启动时协调：进程重启后可以发现并收养之前
      进程遗留的运行中容器
    - is_alive() 方法使用轻量级检查（容器 inspect），而非完整的 HTTP
      健康检查，适合高频轮询场景

辅助函数:
    - wait_for_sandbox_ready(): 轮询沙箱健康端点，直到就绪或超时
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

import requests

from .sandbox_info import SandboxInfo

logger = logging.getLogger(__name__)


def wait_for_sandbox_ready(sandbox_url: str, timeout: int = 30) -> bool:
    """轮询沙箱健康端点，直到就绪或超时。

    通过向沙箱的 /v1/sandbox 端点发送 GET 请求来检查沙箱是否已就绪。
    每秒轮询一次，直到收到 200 响应或超过超时时间。

    Args:
        sandbox_url: 沙箱的 URL 地址（例如 http://k3s:30001）。
        timeout: 最大等待时间（秒），默认为 30 秒。

    Returns:
        如果沙箱在超时内就绪返回 True，否则返回 False。
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
    """沙箱配置后端的抽象基类。

    所有沙箱后端实现都必须继承此类并实现其抽象方法。
    后端负责沙箱容器的创建、销毁、健康检查和发现等操作。

    两种内置实现:
    - LocalContainerBackend: 本地 Docker/Apple Container 模式，管理端口和容器生命周期
    - RemoteSandboxBackend: 远程/K8s 模式，连接到预存在的沙箱 URL 或 Provisioner
    """

    @abstractmethod
    def create(self, thread_id: str, sandbox_id: str, extra_mounts: list[tuple[str, str, bool]] | None = None) -> SandboxInfo:
        """创建/配置一个新的沙箱。

        Args:
            thread_id: 创建沙箱的线程 ID。可用于按线程组织沙箱的后端。
            sandbox_id: 确定性的沙箱标识符，用于生成容器名称。
            extra_mounts: 额外的卷挂载配置，格式为 (host_path, container_path, read_only) 元组。
                不管理容器的后端（例如远程后端）会忽略此参数。

        Returns:
            包含连接详情的 SandboxInfo 实例。
        """
        ...

    @abstractmethod
    def destroy(self, info: SandboxInfo) -> None:
        """销毁/清理一个沙箱并释放其资源。

        Args:
            info: 要销毁的沙箱元数据。
        """
        ...

    @abstractmethod
    def is_alive(self, info: SandboxInfo) -> bool:
        """快速检查沙箱是否仍然存活。

        此方法应是轻量级检查（例如容器 inspect），而非完整的健康检查，
        适合高频轮询场景。

        Args:
            info: 要检查的沙箱元数据。

        Returns:
            如果沙箱看起来仍然存活则返回 True。
        """
        ...

    @abstractmethod
    def discover(self, sandbox_id: str) -> SandboxInfo | None:
        """尝试通过确定性 ID 发现现有沙箱。

        用于跨进程恢复：当另一个进程启动了沙箱时，当前进程可以通过
        确定性的容器名称或 URL 发现它。

        Args:
            sandbox_id: 要查找的确定性沙箱 ID。

        Returns:
            如果找到且健康则返回 SandboxInfo，否则返回 None。
        """
        ...

    def list_running(self) -> list[SandboxInfo]:
        """枚举此后端管理的所有运行中沙箱。

        用于启动时协调：当进程重启时，需要发现之前进程启动的容器，
        以便将它们收养到暖池或在空闲时间过长时销毁。

        默认实现返回空列表，这对于不管理本地容器的后端是正确的
        （例如 RemoteSandboxBackend 将生命周期委托给
        Provisioner，由其处理自己的清理）。

        Returns:
            所有当前运行中沙箱的 SandboxInfo 列表。
        """
        return []
