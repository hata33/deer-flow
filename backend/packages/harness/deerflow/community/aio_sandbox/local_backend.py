"""
本地容器后端 — 基于 Docker 或 Apple Container 的沙箱配置管理

本模块实现了 LocalContainerBackend 类，通过 Docker 或 Apple Container
在本地机器上管理沙箱容器。负责完整的容器生命周期管理，包括启动、停止、
端口分配和跨进程容器发现。

运行时检测策略:
    - macOS: 优先使用 Apple Container（如果可用），否则回退到 Docker
    - 其他平台: 使用 Docker

核心设计决策:
    - 确定性容器命名: 使用 {prefix}-{sandbox_id} 格式的容器名称，
      使多进程间能通过名称发现彼此启动的容器
    - 端口分配带重试: 当 Docker 拒绝端口绑定时自动尝试下一个端口
    - 批量检查优化: 使用单次 docker inspect 调用获取所有容器信息，
      将子进程调用次数从 2N+1 降低到 2
    - 安全的命令日志: 日志中自动遮蔽环境变量值，防止敏感信息泄露
    - Windows 路径兼容: 使用 --mount type=bind 语法避免 Windows 盘符
      路径中的冒号歧义问题

关键算法:
    - _resolve_docker_bind_host(): 根据部署模式选择 Docker 端口绑定的
      主机接口（localhost vs 0.0.0.0），平衡安全性和兼容性
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from datetime import datetime

from deerflow.utils.network import get_free_port, release_port

from .backend import SandboxBackend, wait_for_sandbox_ready
from .sandbox_info import SandboxInfo

logger = logging.getLogger(__name__)


def _parse_docker_timestamp(raw: str) -> float:
    """将 Docker 的 ISO 8601 时间戳解析为 Unix 纪元浮点数。

    Docker 返回的时间戳具有纳秒精度和尾部的 ``Z`` 后缀
    （例如 ``2026-04-08T01:22:50.123456789Z``）。Python 的
    ``fromisoformat`` 最多接受微秒精度，且（3.11 之前）不接受 ``Z``，
    因此在解析前需要对字符串进行规范化处理。

    Args:
        raw: Docker 返回的 ISO 8601 时间戳字符串。

    Returns:
        Unix 纪元时间戳（浮点数）。如果输入为空或解析失败，返回 0.0，
        调用方可将 0.0 作为"未知年龄"的哨兵值。
    """
    if not raw:
        return 0.0
    try:
        s = raw.strip()
        if "." in s:
            dot_pos = s.index(".")
            tz_start = dot_pos + 1
            # 找到小数部分结束位置（非数字字符）
            while tz_start < len(s) and s[tz_start].isdigit():
                tz_start += 1
            # 将纳秒截断为微秒（最多 6 位）
            frac = s[dot_pos + 1 : tz_start][:6]
            tz_suffix = s[tz_start:]
            s = s[: dot_pos + 1] + frac + tz_suffix
        # 将 Z 后缀替换为 +00:00 以兼容 Python fromisoformat
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError) as e:
        logger.debug(f"Could not parse docker timestamp {raw!r}: {e}")
        return 0.0


def _extract_host_port(inspect_entry: dict, container_port: int) -> int | None:
    """从 Docker inspect 条目中提取映射到指定容器端口的主机端口。

    解析 docker inspect 返回的 NetworkSettings.Ports 字段，查找
    指定容器端口的 TCP 绑定。

    Args:
        inspect_entry: docker inspect 返回的单个容器的 JSON 条目。
        container_port: 要查找映射的容器端口号。

    Returns:
        映射到容器端口的主机端口号。如果没有端口映射则返回 None。
    """
    try:
        ports = (inspect_entry.get("NetworkSettings") or {}).get("Ports") or {}
        bindings = ports.get(f"{container_port}/tcp") or []
        if bindings:
            host_port = bindings[0].get("HostPort")
            if host_port:
                return int(host_port)
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _format_container_mount(runtime: str, host_path: str, container_path: str, read_only: bool) -> list[str]:
    """为选定的容器运行时格式化绑定挂载参数。

    Docker 的 ``-v host:container`` 语法对 Windows 盘符路径（如 ``D:/...``）
    存在歧义，因为 ``:`` 既是驱动器分隔符也是卷分隔符。
    因此对 Docker 使用 ``--mount type=bind,...`` 语法来避免解析歧义。
    Apple Container 继续使用 ``-v`` 语法。

    Args:
        runtime: 容器运行时名称（"docker" 或 "container"）。
        host_path: 宿主机上的路径。
        container_path: 容器内的路径。
        read_only: 是否以只读模式挂载。

    Returns:
        格式化后的命令行参数列表。
    """
    if runtime == "docker":
        mount_spec = f"type=bind,src={host_path},dst={container_path}"
        if read_only:
            mount_spec += ",readonly"
        return ["--mount", mount_spec]

    mount_spec = f"{host_path}:{container_path}"
    if read_only:
        mount_spec += ":ro"
    return ["-v", mount_spec]


def _redact_container_command_for_log(cmd: list[str]) -> list[str]:
    """返回环境变量值已遮蔽的 Docker/Container 命令。

    遍历命令行参数列表，将 -e、--env 和 --env= 参数中的环境变量值
    替换为 <redacted>，防止敏感信息（如 API 密钥）泄露到日志中。

    Args:
        cmd: 原始命令行参数列表。

    Returns:
        环境变量值已遮蔽的命令行参数列表。
    """
    redacted: list[str] = []
    redact_next_env = False

    for arg in cmd:
        if redact_next_env:
            # 当前参数是 -e/--env 后面的环境变量值
            if "=" in arg:
                key = arg.split("=", 1)[0]
                redacted.append(f"{key}=<redacted>" if key else "<redacted>")
            else:
                redacted.append(arg)
            redact_next_env = False
            continue

        if arg in {"-e", "--env"}:
            # 标记下一个参数需要遮蔽
            redacted.append(arg)
            redact_next_env = True
            continue

        if arg.startswith("--env="):
            # 内联格式：--env=KEY=VALUE
            value = arg.removeprefix("--env=")
            if "=" in value:
                key = value.split("=", 1)[0]
                redacted.append(f"--env={key}=<redacted>" if key else "--env=<redacted>")
            else:
                redacted.append(arg)
            continue

        redacted.append(arg)

    return redacted


def _format_container_command_for_log(cmd: list[str]) -> str:
    """将命令行参数列表格式化为可记录的字符串。

    在 Windows 上使用 subprocess.list2cmdline，在其他平台上使用 shlex.join。

    Args:
        cmd: 命令行参数列表。

    Returns:
        格式化后的命令行字符串。
    """
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return shlex.join(cmd)


def _normalize_sandbox_host(host: str) -> str:
    """规范化沙箱主机地址（去除空白并转为小写）。

    Args:
        host: 原始主机地址字符串。

    Returns:
        规范化后的主机地址。
    """
    return host.strip().lower()


def _is_ipv6_loopback_sandbox_host(host: str) -> bool:
    """判断是否为 IPv6 本地回环地址。

    Args:
        host: 主机地址字符串。

    Returns:
        如果是 IPv6 本地回环地址返回 True。
    """
    return _normalize_sandbox_host(host) in {"::1", "[::1]"}


def _is_loopback_sandbox_host(host: str) -> bool:
    """判断是否为本地回环地址（IPv4 或 IPv6）。

    Args:
        host: 主机地址字符串。

    Returns:
        如果是本地回环地址返回 True。
    """
    return _normalize_sandbox_host(host) in {"", "localhost", "127.0.0.1", "::1", "[::1]"}


def _resolve_docker_bind_host(sandbox_host: str | None = None, bind_host: str | None = None) -> str:
    """为 Docker 的 ``-p`` 端口发布选择主机绑定接口。

    策略说明:
    - 裸机/本地运行通过 localhost 与沙箱通信，不应在所有主机接口上暴露
      沙箱 HTTP API
    - Docker-outside-of-Docker（DooD）部署通常从另一个容器通过
      ``host.docker.internal`` 访问沙箱；保持其传统的广泛绑定（0.0.0.0），
      除非运维人员通过 ``DEER_FLOW_SANDBOX_BIND_HOST`` 选择更窄的绑定
    - 当运维人员选择 IPv6 本地回环沙箱主机时，Docker 也绑定到 IPv6
      本地回环，确保广播的沙箱 URL 和发布的套接字使用相同的地址族

    优先级（从高到低）:
    1. 显式 bind_host 参数或 DEER_FLOW_SANDBOX_BIND_HOST 环境变量
    2. IPv6 本地回环沙箱主机 → [::1]
    3. IPv4 本地回环沙箱主机 → 127.0.0.1
    4. 非本地回环沙箱主机 → 0.0.0.0（兼容性默认值）

    Args:
        sandbox_host: 沙箱主机地址（可选）。
        bind_host: 显式绑定主机地址（可选）。

    Returns:
        Docker 端口绑定使用的主机接口地址。
    """
    # 优先使用显式绑定点
    explicit_bind = bind_host if bind_host is not None else os.environ.get("DEER_FLOW_SANDBOX_BIND_HOST")
    if explicit_bind is not None:
        explicit_bind = explicit_bind.strip()
        if explicit_bind:
            logger.debug("Docker sandbox bind: %s (explicit bind host override)", explicit_bind)
            return explicit_bind

    # 根据沙箱主机的地址族决定绑定接口
    host = sandbox_host if sandbox_host is not None else os.environ.get("DEER_FLOW_SANDBOX_HOST", "localhost")
    if _is_ipv6_loopback_sandbox_host(host):
        logger.debug("Docker sandbox bind: [::1] (IPv6 loopback sandbox host)")
        return "[::1]"
    if _is_loopback_sandbox_host(host):
        logger.debug("Docker sandbox bind: 127.0.0.1 (loopback default)")
        return "127.0.0.1"

    # 非本地回环地址使用广泛绑定以保证兼容性
    logger.debug("Docker sandbox bind: 0.0.0.0 (non-loopback sandbox host compatibility)")
    return "0.0.0.0"


class LocalContainerBackend(SandboxBackend):
    """使用 Docker 或 Apple Container 在本地管理沙箱容器的后端。

    在 macOS 上自动优先使用 Apple Container（如果可用），否则回退到 Docker。
    在其他平台上使用 Docker。

    特性:
    - 确定性容器命名，支持跨进程发现
    - 线程安全的端口分配工具
    - 完整的容器生命周期管理（启动/停止，使用 --rm 自动清理）
    - 支持卷挂载和环境变量注入
    - 批量容器检查优化（单次子进程调用获取所有容器信息）
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
            image: 使用的容器镜像名称/标签。
            base_port: 端口搜索的起始端口号。
            container_prefix: 容器名称前缀（例如 "deer-flow-sandbox"）。
            config_mounts: 来自配置的卷挂载配置（VolumeMountConfig 列表）。
            environment: 注入到容器中的环境变量。
        """
        self._image = image
        self._base_port = base_port
        self._container_prefix = container_prefix
        self._config_mounts = config_mounts
        self._environment = environment
        self._runtime = self._detect_runtime()

    @property
    def runtime(self) -> str:
        """检测到的容器运行时名称（"docker" 或 "container"）。"""
        return self._runtime

    def _detect_runtime(self) -> str:
        """检测应使用的容器运行时。

        在 macOS 上优先使用 Apple Container（如果可用），否则回退到 Docker。
        在其他平台上使用 Docker。

        Returns:
            "container" 表示 Apple Container，"docker" 表示 Docker。
        """
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
        """启动新容器并返回其连接信息。

        使用重试循环处理端口冲突：当 Docker 拒绝端口绑定时（例如进程
        重启后旧容器仍占用端口），自动尝试下一个端口。如果容器名称
        冲突（另一个进程已启动了同 ID 的容器），尝试发现并收养现有容器。

        Args:
            thread_id: 创建沙箱的线程 ID。
            sandbox_id: 确定性的沙箱标识符（用于容器命名）。
            extra_mounts: 额外的卷挂载配置，格式为 (host_path, container_path, read_only) 元组。

        Returns:
            包含容器详情的 SandboxInfo 实例。

        Raises:
            RuntimeError: 如果容器启动失败。
        """
        container_name = f"{self._container_prefix}-{sandbox_id}"

        # 重试循环：当 Docker 拒绝端口时（例如进程重启后旧容器仍占用绑定），
        # 跳过该端口并尝试下一个。get_free_port 中的套接字绑定检查模拟了
        # Docker 的 0.0.0.0 绑定，但 Docker 的端口释放可能是异步的，
        # 因此这里的响应式回退确保了始终能够取得进展。
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
                # 端口已被占用：跳过此端口并使用下一个端口重试
                if "port is already allocated" in err or "address already in use" in err_lower:
                    logger.warning(f"Port {port} rejected by Docker (already allocated), retrying with next port")
                    _next_start = port + 1
                    continue
                # 容器名称冲突：另一个进程可能已经为该 sandbox_id 启动了确定性
                # 容器。尝试发现并收养现有容器，而非直接失败。
                if "is already in use by container" in err_lower or "conflict. the container name" in err_lower:
                    logger.warning(f"Container name {container_name} already in use, attempting to discover existing sandbox instance")
                    existing = self.discover(sandbox_id)
                    if existing is not None:
                        return existing
                raise
        else:
            raise RuntimeError("Could not start sandbox container: all candidate ports are already allocated by Docker")

        # 在 Docker 内部运行时（DooD），沙箱容器通过 host.docker.internal
        # 而非 localhost 可达（它们运行在宿主机的 Docker 守护进程上）
        sandbox_host = os.environ.get("DEER_FLOW_SANDBOX_HOST", "localhost")
        return SandboxInfo(
            sandbox_id=sandbox_id,
            sandbox_url=f"http://{sandbox_host}:{port}",
            container_name=container_name,
            container_id=container_id,
        )

    def destroy(self, info: SandboxInfo) -> None:
        """停止容器并释放其端口。

        优先使用 container_id，回退到 container_name（两者都被 docker stop 接受）。
        这确保了通过 list_running() 发现的容器（仅有名称）也能被停止。

        Args:
            info: 要销毁的沙箱元数据。
        """
        stop_target = info.container_id or info.container_name
        if stop_target:
            self._stop_container(stop_target)
        # 从 sandbox_url 中提取端口号进行释放
        try:
            from urllib.parse import urlparse

            port = urlparse(info.sandbox_url).port
            if port:
                release_port(port)
        except Exception:
            pass

    def is_alive(self, info: SandboxInfo) -> bool:
        """检查容器是否仍在运行（轻量级，无 HTTP 请求）。

        Args:
            info: 要检查的沙箱元数据。

        Returns:
            如果容器名称存在且正在运行返回 True，否则返回 False。
        """
        if info.container_name:
            return self._is_container_running(info.container_name)
        return False

    def discover(self, sandbox_id: str) -> SandboxInfo | None:
        """通过确定性名称发现现有容器。

        检查具有预期名称的容器是否正在运行，获取其端口映射，
        并验证其对健康检查的响应。

        Args:
            sandbox_id: 确定性的沙箱 ID（决定容器名称）。

        Returns:
            如果找到容器且健康则返回 SandboxInfo，否则返回 None。
        """
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

    def list_running(self) -> list[SandboxInfo]:
        """枚举所有匹配配置前缀的运行中容器。

        使用单次 ``docker ps`` 调用列出容器名称，然后使用单次批量
        ``docker inspect`` 调用同时获取所有容器的创建时间戳和端口映射。
        总子进程调用次数：2（相比朴素方法的 2N+1）。

        注意: Docker 的 ``--filter name=`` 执行 *子字符串* 匹配，
        因此应用了辅助的 ``startswith`` 检查确保仅包含具有精确前缀的容器。

        没有端口映射的容器仍会被包含在内（sandbox_url 为空），以便
        启动协调能够收养孤儿容器，而不受其端口状态的影响。

        Returns:
            所有匹配前缀的运行中沙箱的 SandboxInfo 列表。
        """
        # 步骤 1: 通过 docker ps 枚举容器名称
        try:
            result = subprocess.run(
                [
                    self._runtime,
                    "ps",
                    "--filter",
                    f"name={self._container_prefix}-",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                logger.warning(
                    "Failed to list running containers with %s ps (returncode=%s, stderr=%s)",
                    self._runtime,
                    result.returncode,
                    stderr or "<empty>",
                )
                return []
            if not result.stdout.strip():
                return []
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning(f"Failed to list running containers: {e}")
            return []

        # 过滤出精确匹配前缀的容器名称（Docker filter 是基于子字符串的）
        container_names = [name.strip() for name in result.stdout.strip().splitlines() if name.strip().startswith(self._container_prefix + "-")]
        if not container_names:
            return []

        # 步骤 2: 批量 docker inspect — 单次子进程调用获取所有容器信息
        inspections = self._batch_inspect(container_names)

        infos: list[SandboxInfo] = []
        sandbox_host = os.environ.get("DEER_FLOW_SANDBOX_HOST", "localhost")
        for container_name in container_names:
            data = inspections.get(container_name)
            if data is None:
                # 容器在 ps 和 inspect 之间消失了，或 inspect 失败
                continue
            created_at, host_port = data
            # 从容器名称中提取 sandbox_id（去除前缀部分）
            sandbox_id = container_name[len(self._container_prefix) + 1 :]
            sandbox_url = f"http://{sandbox_host}:{host_port}" if host_port else ""

            infos.append(
                SandboxInfo(
                    sandbox_id=sandbox_id,
                    sandbox_url=sandbox_url,
                    container_name=container_name,
                    created_at=created_at,
                )
            )

        logger.info(f"Found {len(infos)} running sandbox container(s)")
        return infos

    def _batch_inspect(self, container_names: list[str]) -> dict[str, tuple[float, int | None]]:
        """在单次子进程调用中批量检查容器。

        使用单次 docker inspect 调用获取所有容器的创建时间戳和端口映射，
        避免对每个容器单独调用 inspect 带来的性能开销。

        Args:
            container_names: 要检查的容器名称列表。

        Returns:
            ``container_name -> (created_at, host_port)`` 的映射字典。
            缺失的容器或解析失败会从结果中静默排除。
        """
        if not container_names:
            return {}
        try:
            result = subprocess.run(
                [self._runtime, "inspect", *container_names],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning(f"Failed to batch-inspect containers: {e}")
            return {}

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            logger.warning(
                "Failed to batch-inspect containers with %s inspect (returncode=%s, stderr=%s)",
                self._runtime,
                result.returncode,
                stderr or "<empty>",
            )
            return {}

        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse docker inspect output as JSON: {e}")
            return {}

        out: dict[str, tuple[float, int | None]] = {}
        for entry in payload:
            # docker inspect 响应中 ``Name`` 以 ``/`` 前缀开头
            name = (entry.get("Name") or "").lstrip("/")
            if not name:
                continue
            created_at = _parse_docker_timestamp(entry.get("Created", ""))
            host_port = _extract_host_port(entry, 8080)
            out[name] = (created_at, host_port)
        return out

    # ── 容器操作 ──────────────────────────────────────────────────────────

    def _start_container(
        self,
        container_name: str,
        port: int,
        extra_mounts: list[tuple[str, str, bool]] | None = None,
    ) -> str:
        """启动新容器。

        构建并执行完整的容器启动命令，包括端口映射、环境变量注入、
        卷挂载等配置。

        Args:
            container_name: 容器名称。
            port: 映射到容器端口 8080 的主机端口。
            extra_mounts: 额外的卷挂载配置。

        Returns:
            容器 ID。

        Raises:
            RuntimeError: 如果容器启动失败。
        """
        cmd = [self._runtime, "run"]

        # Docker 特定的安全选项
        if self._runtime == "docker":
            cmd.extend(["--security-opt", "seccomp=unconfined"])

        # 根据运行时选择端口映射格式
        if self._runtime == "docker":
            port_mapping = f"{_resolve_docker_bind_host()}:{port}:8080"
        else:
            port_mapping = f"{port}:8080"

        cmd.extend(
            [
                "--rm",    # 容器停止时自动删除
                "-d",      # 后台运行（detached 模式）
                "-p",
                port_mapping,
                "--name",
                container_name,
            ]
        )

        # 注入环境变量
        for key, value in self._environment.items():
            cmd.extend(["-e", f"{key}={value}"])

        # 配置级别的卷挂载
        for mount in self._config_mounts:
            cmd.extend(
                _format_container_mount(
                    self._runtime,
                    mount.host_path,
                    mount.container_path,
                    mount.read_only,
                )
            )

        # 额外挂载（线程特定、技能等）
        if extra_mounts:
            for host_path, container_path, read_only in extra_mounts:
                cmd.extend(
                    _format_container_mount(
                        self._runtime,
                        host_path,
                        container_path,
                        read_only,
                    )
                )

        cmd.append(self._image)

        # 记录命令时遮蔽敏感的环境变量值
        log_cmd = _format_container_command_for_log(_redact_container_command_for_log(cmd))
        logger.info(f"Starting container using {self._runtime}: {log_cmd}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            container_id = result.stdout.strip()
            logger.info(f"Started container {container_name} (ID: {container_id}) using {self._runtime}")
            return container_id
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to start container using {self._runtime}: {e.stderr}")
            raise RuntimeError(f"Failed to start sandbox container: {e.stderr}")

    def _stop_container(self, container_id: str) -> None:
        """停止容器（--rm 确保自动删除）。

        Args:
            container_id: 容器 ID 或名称。
        """
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
        """检查指定容器是否正在运行。

        通过 docker inspect 获取容器的 State.Running 字段。
        这实现了跨进程容器发现 — 任何进程都可以通过确定性的容器名称
        检测到另一个进程启动的容器。

        Args:
            container_name: 要检查的容器名称。

        Returns:
            如果容器正在运行返回 True，否则返回 False。
        """
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
        """获取运行中容器的主机端口映射。

        通过 docker port 命令查询容器的端口映射关系。

        Args:
            container_name: 要检查的容器名称。

        Returns:
            映射到容器端口 8080 的主机端口号。如果未找到返回 None。
        """
        try:
            result = subprocess.run(
                [self._runtime, "port", container_name, "8080"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                # 输出格式: "0.0.0.0:PORT" 或 ":::PORT"
                port_str = result.stdout.strip().split(":")[-1]
                return int(port_str)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            pass
        return None
