"""远程沙箱后端。

通过 Provisioner 服务管理 K8s Pod 生命周期。
Provisioner 动态创建 per-sandbox-id 的 Pod + NodePort Service，
本后端通过 k3s:{NodePort} 直接访问沙箱 Pod。

架构:
    ┌────────────┐  HTTP   ┌─────────────┐  K8s API  ┌──────────┐
    │  本模块    │ ──────▸ │ provisioner │ ────────▸ │   k3s    │
    │ (backend)  │         │   :8002     │           │  :6443   │
    └────────────┘         └─────────────┘           └─────┬────┘
                                                           │ 创建
                            ┌─────────────┐          ┌─────▼──────┐
                            │   backend   │ ───────▸ │  sandbox   │
                            │             │  直连    │  Pod(s)    │
                            └─────────────┘ k3s:NPort └────────────┘
"""

from __future__ import annotations

import logging

import requests

from .backend import SandboxBackend
from .sandbox_info import SandboxInfo

logger = logging.getLogger(__name__)


class RemoteSandboxBackend(SandboxBackend):
    """远程沙箱后端：将沙箱生命周期委托给 Provisioner 服务。

    所有 Pod 创建、销毁、发现均由 Provisioner 处理，本模块是轻量 HTTP 客户端。
    """

    def __init__(self, provisioner_url: str):
        """初始化远程后端。

        Args:
            provisioner_url: Provisioner 服务地址（如 http://provisioner:8002）。
        """
        self._provisioner_url = provisioner_url.rstrip("/")

    @property
    def provisioner_url(self) -> str:
        return self._provisioner_url

    # ── SandboxBackend 接口实现 ──────────────────────────────────────────

    def create(
        self,
        thread_id: str,
        sandbox_id: str,
        extra_mounts: list[tuple[str, str, bool]] | None = None,
    ) -> SandboxInfo:
        """通过 Provisioner 创建沙箱 Pod + Service（POST /api/sandboxes）。"""
        return self._provisioner_create(thread_id, sandbox_id, extra_mounts)

    def destroy(self, info: SandboxInfo) -> None:
        """通过 Provisioner 销毁沙箱 Pod + Service。"""
        self._provisioner_destroy(info.sandbox_id)

    def is_alive(self, info: SandboxInfo) -> bool:
        """通过 Provisioner 检查沙箱 Pod 是否运行中。"""
        return self._provisioner_is_alive(info.sandbox_id)

    def discover(self, sandbox_id: str) -> SandboxInfo | None:
        """通过 Provisioner 发现已有沙箱（GET /api/sandboxes/{sandbox_id}）。"""
        return self._provisioner_discover(sandbox_id)

    # ── Provisioner API 调用 ─────────────────────────────────────────────

    def _provisioner_create(self, thread_id: str, sandbox_id: str, extra_mounts: list[tuple[str, str, bool]] | None = None) -> SandboxInfo:
        """创建沙箱。"""
        try:
            resp = requests.post(
                f"{self._provisioner_url}/api/sandboxes",
                json={
                    "sandbox_id": sandbox_id,
                    "thread_id": thread_id,
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
        """销毁沙箱。"""
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
        """检查沙箱存活状态。"""
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
        """发现已有沙箱。"""
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
