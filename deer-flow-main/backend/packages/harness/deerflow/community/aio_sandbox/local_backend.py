"""本地容器后端。

使用 Docker 或 Apple Container 在本地管理沙箱容器。
处理容器生命周期、端口分配和跨进程容器发现。
"""

from __future__ import annotations

import logging
import os
import subprocess

from deerflow.utils.network import get_free_port, release_port

from .backend import SandboxBackend, wait_for_sandbox_ready
from .sandbox_info import SandboxInfo

logger = logging.getLogger(__name__)


class LocalContainerBackend(SandboxBackend):
    """本地容器后端：通过 Docker 或 Apple Container 管理沙箱容器。

    macOS 优先使用 Apple Container（如可用），否则回退 Docker。
    其他平台使用 Docker。

    特性：
    - 确定性容器命名（跨进程发现）
    - 线程安全端口分配
    - 容器生命周期管理（启动/停止，--rm 自动清理）
    - 卷挂载和环境变量注入
    """

    def __init__(
        self,
        *,
        image: str,
        base_port: int,
        container_prefix: str,
        config_mounts: list,
        environment: dict[str, str],
    ):
        """初始化本地容器后端。

        Args:
            image: 容器镜像。
            base_port: 端口搜索起始值。
            container_prefix: 容器名前缀（如 "deer-flow-sandbox"）。
            config_mounts: 配置中的卷挂载（VolumeMountConfig 列表）。
            environment: 注入容器的环境变量。
        """
        self._image = image
        self._base_port = base_port
        self._container_prefix = container_prefix
        self._config_mounts = config_mounts
        self._environment = environment
        self._runtime = self._detect_runtime()

    @property
    def runtime(self) -> str:
        """检测到的容器运行时（"docker" 或 "container"）。"""
        return self._runtime

    def _detect_runtime(self) -> str:
        """检测容器运行时。macOS 优先 Apple Container，否则 Docker。"""
        import platform

        if platform.system() == "Darwin":
            try:
                result = subprocess.run(
                    ["container", "--version"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                logger.info(f"Detected Apple Container: {result.stdout.strip()}")
                return "container"
            except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                logger.info("Apple Container not available, falling back to Docker")

        return "docker"

    # ── SandboxBackend 接口实现 ──────────────────────────────────────────

    def create(self, thread_id: str, sandbox_id: str, extra_mounts: list[tuple[str, str, bool]] | None = None) -> SandboxInfo:
        """启动新容器并返回连接信息。

        包含端口冲突重试逻辑：Docker 端口释放可能异步延迟，
        导致 get_free_port 认为可用但 Docker 实际仍占用。
        """
        container_name = f"{self._container_prefix}-{sandbox_id}"

        # 重试循环：端口冲突时跳过并尝试下一个端口
        _next_start = self._base_port
        container_id: str | None = None
        port: int = 0
        for _attempt in range(10):
            port = get_free_port(start_port=_next_start)
            try:
                container_id = self._start_container(container_name, port, extra_mounts)
                break
            except RuntimeError as exc:
                release_port(port)
                err = str(exc)
                err_lower = err.lower()
                # 端口已被占用：跳过并重试
                if "port is already allocated" in err or "address already in use" in err_lower:
                    logger.warning(f"Port {port} rejected by Docker (already allocated), retrying with next port")
                    _next_start = port + 1
                    continue
                # 容器名冲突：另一个进程可能已启动了同 ID 的容器，尝试发现并复用
                if "is already in use by container" in err_lower or "conflict. the container name" in err_lower:
                    logger.warning(f"Container name {container_name} already in use, attempting to discover existing sandbox instance")
                    existing = self.discover(sandbox_id)
                    if existing is not None:
                        return existing
                raise
        else:
            raise RuntimeError("Could not start sandbox container: all candidate ports are already allocated by Docker")

        # Docker-in-Docker 场景：沙箱容器通过 host.docker.internal 可达
        sandbox_host = os.environ.get("DEER_FLOW_SANDBOX_HOST", "localhost")
        return SandboxInfo(
            sandbox_id=sandbox_id,
            sandbox_url=f"http://{sandbox_host}:{port}",
            container_name=container_name,
            container_id=container_id,
        )

    def destroy(self, info: SandboxInfo) -> None:
        """停止容器并释放端口。"""
        if info.container_id:
            self._stop_container(info.container_id)
        try:
            from urllib.parse import urlparse

            port = urlparse(info.sandbox_url).port
            if port:
                release_port(port)
        except Exception:
            pass

    def is_alive(self, info: SandboxInfo) -> bool:
        """检查容器是否仍在运行（轻量检查，无 HTTP 请求）。"""
        if info.container_name:
            return self._is_container_running(info.container_name)
        return False

    def discover(self, sandbox_id: str) -> SandboxInfo | None:
        """通过确定性容器名发现已有容器。"""
        container_name = f"{self._container_prefix}-{sandbox_id}"

        if not self._is_container_running(container_name):
            return None

        port = self._get_container_port(container_name)
        if port is None:
            return None

        sandbox_host = os.environ.get("DEER_FLOW_SANDBOX_HOST", "localhost")
        sandbox_url = f"http://{sandbox_host}:{port}"
        if not wait_for_sandbox_ready(sandbox_url, timeout=5):
            return None

        return SandboxInfo(
            sandbox_id=sandbox_id,
            sandbox_url=sandbox_url,
            container_name=container_name,
        )

    # ── 容器操作 ─────────────────────────────────────────────

    def _start_container(
        self,
        container_name: str,
        port: int,
        extra_mounts: list[tuple[str, str, bool]] | None = None,
    ) -> str:
        """启动容器实例，返回容器 ID。"""
        cmd = [self._runtime, "run"]

        # Docker 安全选项
        if self._runtime == "docker":
            cmd.extend(["--security-opt", "seccomp=unconfined"])

        cmd.extend(
            [
                "--rm",  # 停止时自动删除
                "-d",  # 后台运行
                "-p",
                f"{port}:8080",
                "--name",
                container_name,
            ]
        )

        # 环境变量
        for key, value in self._environment.items():
            cmd.extend(["-e", f"{key}={value}"])

        # 配置级卷挂载
        for mount in self._config_mounts:
            mount_spec = f"{mount.host_path}:{mount.container_path}"
            if mount.read_only:
                mount_spec += ":ro"
            cmd.extend(["-v", mount_spec])

        # 额外挂载（线程特定目录、skills 等）
        if extra_mounts:
            for host_path, container_path, read_only in extra_mounts:
                mount_spec = f"{host_path}:{container_path}"
                if read_only:
                    mount_spec += ":ro"
                cmd.extend(["-v", mount_spec])

        cmd.append(self._image)

        logger.info(f"Starting container using {self._runtime}: {' '.join(cmd)}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            container_id = result.stdout.strip()
            logger.info(f"Started container {container_name} (ID: {container_id}) using {self._runtime}")
            return container_id
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to start container using {self._runtime}: {e.stderr}")
            raise RuntimeError(f"Failed to start sandbox container: {e.stderr}")

    def _stop_container(self, container_id: str) -> None:
        """停止容器（--rm 确保自动清理）。"""
        try:
            subprocess.run(
                [self._runtime, "stop", container_id],
                capture_output=True,
                text=True,
                check=True,
            )
            logger.info(f"Stopped container {container_id} using {self._runtime}")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to stop container {container_id}: {e.stderr}")

    def _is_container_running(self, container_name: str) -> bool:
        """检查命名容器是否正在运行（通过确定性名称实现跨进程发现）。"""
        try:
            result = subprocess.run(
                [self._runtime, "inspect", "-f", "{{.State.Running}}", container_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0 and result.stdout.strip().lower() == "true"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    def _get_container_port(self, container_name: str) -> int | None:
        """获取容器的宿主机端口映射（容器端口 8080）。"""
        try:
            result = subprocess.run(
                [self._runtime, "port", container_name, "8080"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                # 输出格式："0.0.0.0:PORT" 或 ":::PORT"
                port_str = result.stdout.strip().split(":")[-1]
                return int(port_str)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            pass
        return None
