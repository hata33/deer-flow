"""
远程沙箱后端 — 将 Pod 生命周期委托给 Provisioner 服务

本模块实现了 RemoteSandboxBackend 类，通过 HTTP API 与 Provisioner
服务交互来管理 Kubernetes 中的沙箱 Pod。

架构示意:
    ┌────────────┐  HTTP   ┌─────────────┐  K8s API  ┌──────────┐
    │  本模块     │ ──────▸ │ provisioner │ ────────▸ │   k3s    │
    │ (后端)     │         │ :8002       │           │ :6443    │
    └────────────┘         └─────────────┘           └─────┬────┘
                                                           │ 创建
                           ┌─────────────┐           ┌─────▼──────┐
                           │   后端      │ ────────▸ │  沙箱      │
                           │             │  直接     │  Pod(s)    │
                           └─────────────┘ k3s:NPort └────────────┘

Provisioner 动态地为每个 sandbox_id 创建 Pod + NodePort Service。
本后端通过 k3s NodePort 直接访问沙箱 Pod。

所有 Pod 的创建、销毁和发现操作都由 Provisioner 处理，
本后端是一个轻量级的 HTTP 客户端封装。

配置示例 (config.yaml):
    sandbox:
        use: deerflow.community.aio_sandbox:AioSandboxProvider
        provisioner_url: http://provisioner:8002
"""

from __future__ import annotations

import logging

import requests

from deerflow.runtime.user_context import get_effective_user_id

from .backend import SandboxBackend
from .sandbox_info import SandboxInfo

logger = logging.getLogger(__name__)


class RemoteSandboxBackend(SandboxBackend):
    """将沙箱生命周期委托给 Provisioner 服务的后端。

    所有 Pod 的创建、销毁和发现操作都由 Provisioner 处理。
    本后端是一个轻量级的 HTTP 客户端。

    典型配置 (config.yaml)::
        sandbox:
            use: deerflow.community.aio_sandbox:AioSandboxProvider
            provisioner_url: http://provisioner:8002
    """

    def __init__(self, provisioner_url: str):
        """使用 Provisioner 服务 URL 初始化后端。

        Args:
            provisioner_url: Provisioner 服务的 URL 地址
                             （例如 ``http://provisioner:8002``）。
        """
        self._provisioner_url = provisioner_url.rstrip("/")

    @property
    def provisioner_url(self) -> str:
        """获取 Provisioner 服务的 URL 地址。

        Returns:
            Provisioner 服务的 URL 字符串。
        """
        return self._provisioner_url

    # ── SandboxBackend 接口实现 ──────────────────────────────────────────

    def create(
        self,
        thread_id: str,
        sandbox_id: str,
        extra_mounts: list[tuple[str, str, bool]] | None = None,
    ) -> SandboxInfo:
        """通过 Provisioner 创建沙箱 Pod + Service。

        调用 ``POST /api/sandboxes`` 在 k3s 中创建专用的 Pod +
        NodePort Service。

        Args:
            thread_id: 创建沙箱的线程 ID。
            sandbox_id: 确定性的沙箱标识符。
            extra_mounts: 额外的卷挂载配置（远程后端忽略此参数）。

        Returns:
            包含沙箱连接详情的 SandboxInfo 实例。

        Raises:
            RuntimeError: 如果 Provisioner 创建失败。
        """
        return self._provisioner_create(thread_id, sandbox_id, extra_mounts)

    def destroy(self, info: SandboxInfo) -> None:
        """通过 Provisioner 销毁沙箱 Pod + Service。

        Args:
            info: 要销毁的沙箱元数据。
        """
        self._provisioner_destroy(info.sandbox_id)

    def is_alive(self, info: SandboxInfo) -> bool:
        """检查沙箱 Pod 是否仍在运行。

        Args:
            info: 要检查的沙箱元数据。

        Returns:
            如果 Pod 状态为 "Running" 返回 True，否则返回 False。
        """
        return self._provisioner_is_alive(info.sandbox_id)

    def discover(self, sandbox_id: str) -> SandboxInfo | None:
        """通过 Provisioner 发现现有沙箱。

        调用 ``GET /api/sandboxes/{sandbox_id}`` 查询 Pod 是否存在。

        Args:
            sandbox_id: 要查找的确定性沙箱 ID。

        Returns:
            如果 Pod 存在则返回 SandboxInfo，否则返回 None。
        """
        return self._provisioner_discover(sandbox_id)

    def list_running(self) -> list[SandboxInfo]:
        """返回 Provisioner 当前管理的所有沙箱。

        调用 ``GET /api/sandboxes`` 使 ``AioSandboxProvider._reconcile_orphans()``
        能够收养之前进程创建但从未显式销毁的 Pod。

        如果没有此功能，进程重启会默默地使所有现有的 k8s Pod 成为
        孤儿 — 它们会永远运行下去，因为空闲检查器仅跟踪进程内状态。

        Returns:
            所有当前运行中沙箱的 SandboxInfo 列表。
        """
        return self._provisioner_list()

    # ── Provisioner API 调用 ─────────────────────────────────────────────

    def _provisioner_list(self) -> list[SandboxInfo]:
        """调用 GET /api/sandboxes 列出所有运行中的沙箱。

        解析 Provisioner 返回的 JSON 响应，提取有效的沙箱信息。
        对每个沙箱条目进行类型验证，跳过无效数据。

        Returns:
            运行中沙箱的 SandboxInfo 列表。如果请求失败返回空列表。
        """
        try:
            resp = requests.get(f"{self._provisioner_url}/api/sandboxes", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                logger.warning("Provisioner list_running returned non-dict payload: %r", type(data))
                return []

            sandboxes = data.get("sandboxes", [])
            if not isinstance(sandboxes, list):
                logger.warning("Provisioner list_running returned non-list sandboxes: %r", type(sandboxes))
                return []

            infos: list[SandboxInfo] = []
            for sandbox in sandboxes:
                if not isinstance(sandbox, dict):
                    logger.warning("Provisioner list_running entry is not a dict: %r", type(sandbox))
                    continue

                sandbox_id = sandbox.get("sandbox_id")
                sandbox_url = sandbox.get("sandbox_url")
                # 仅接受有效的字符串类型 ID 和 URL
                if isinstance(sandbox_id, str) and sandbox_id and isinstance(sandbox_url, str) and sandbox_url:
                    infos.append(SandboxInfo(sandbox_id=sandbox_id, sandbox_url=sandbox_url))

            logger.info("Provisioner list_running: %d sandbox(es) found", len(infos))
            return infos
        except requests.RequestException as exc:
            logger.warning("Provisioner list_running failed: %s", exc)
            return []

    def _provisioner_create(self, thread_id: str, sandbox_id: str, extra_mounts: list[tuple[str, str, bool]] | None = None) -> SandboxInfo:
        """调用 POST /api/sandboxes 创建 Pod + Service。

        向 Provisioner 发送创建请求，包含沙箱 ID、线程 ID 和用户 ID。
        Provisioner 会在 k3s 中创建对应的 Pod 和 NodePort Service。

        Args:
            thread_id: 线程 ID。
            sandbox_id: 沙箱 ID。
            extra_mounts: 额外挂载（远程后端忽略）。

        Returns:
            包含沙箱连接信息的 SandboxInfo 实例。

        Raises:
            RuntimeError: 如果 Provisioner 返回错误。
        """
        try:
            resp = requests.post(
                f"{self._provisioner_url}/api/sandboxes",
                json={
                    "sandbox_id": sandbox_id,
                    "thread_id": thread_id,
                    "user_id": get_effective_user_id(),
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"Provisioner created sandbox {sandbox_id}: sandbox_url={data['sandbox_url']}")
            return SandboxInfo(
                sandbox_id=sandbox_id,
                sandbox_url=data["sandbox_url"],
            )
        except requests.RequestException as exc:
            logger.error(f"Provisioner create failed for {sandbox_id}: {exc}")
            raise RuntimeError(f"Provisioner create failed: {exc}") from exc

    def _provisioner_destroy(self, sandbox_id: str) -> None:
        """调用 DELETE /api/sandboxes/{sandbox_id} 销毁 Pod + Service。

        Args:
            sandbox_id: 要销毁的沙箱 ID。
        """
        try:
            resp = requests.delete(
                f"{self._provisioner_url}/api/sandboxes/{sandbox_id}",
                timeout=15,
            )
            if resp.ok:
                logger.info(f"Provisioner destroyed sandbox {sandbox_id}")
            else:
                logger.warning(f"Provisioner destroy returned {resp.status_code}: {resp.text}")
        except requests.RequestException as exc:
            logger.warning(f"Provisioner destroy failed for {sandbox_id}: {exc}")

    def _provisioner_is_alive(self, sandbox_id: str) -> bool:
        """调用 GET /api/sandboxes/{sandbox_id} 检查 Pod 阶段。

        Args:
            sandbox_id: 要检查的沙箱 ID。

        Returns:
            如果 Pod 状态为 "Running" 返回 True，否则返回 False。
        """
        try:
            resp = requests.get(
                f"{self._provisioner_url}/api/sandboxes/{sandbox_id}",
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                return data.get("status") == "Running"
            return False
        except requests.RequestException:
            return False

    def _provisioner_discover(self, sandbox_id: str) -> SandboxInfo | None:
        """调用 GET /api/sandboxes/{sandbox_id} 发现现有沙箱。

        Args:
            sandbox_id: 要发现的沙箱 ID。

        Returns:
            如果沙箱存在返回 SandboxInfo，404 或错误时返回 None。
        """
        try:
            resp = requests.get(
                f"{self._provisioner_url}/api/sandboxes/{sandbox_id}",
                timeout=10,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            return SandboxInfo(
                sandbox_id=sandbox_id,
                sandbox_url=data["sandbox_url"],
            )
        except requests.RequestException as exc:
            logger.debug(f"Provisioner discover failed for {sandbox_id}: {exc}")
            return None
