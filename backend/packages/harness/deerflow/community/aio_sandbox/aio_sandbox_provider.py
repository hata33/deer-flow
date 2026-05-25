"""
AIO Sandbox Provider — 沙箱生命周期编排器，支持可插拔后端

本模块是 DeerFlow 沙箱子系统的核心编排层，负责管理沙箱实例的完整生命周期。
Provider 通过组合后端（SandboxBackend）来实现不同部署模式下的沙箱管理。

核心职责:
    - 沙箱实例的进程内缓存，实现快速重复访问
    - 基于空闲超时的自动回收机制
    - 信号处理和优雅关机
    - 挂载点计算（线程特定的数据目录、技能目录等）
    - 跨进程沙箱发现与协调（通过确定性 ID 和文件锁）

两层一致性架构:
    Layer 1: 进程内缓存 — 最快路径，覆盖同一进程内的重复访问
    Layer 2: 后端发现 — 覆盖其他进程启动的容器；sandbox_id 从 thread_id
             确定性派生，无需共享状态文件，任何进程都能推导出相同的容器名

暖池（Warm Pool）机制:
    当沙箱被 release() 后不会立即停止容器，而是进入暖池。下次同一线程
    请求沙箱时可从暖池直接回收，避免冷启动延迟。暖池中的容器仅在以下
    情况被销毁：replicas 容量超限时 LRU 淘汰、空闲超时、或系统关闭。

部署模式:
    - 本地模式: 使用 LocalContainerBackend，直接管理 Docker/Apple Container
    - 远程模式: 使用 RemoteSandboxBackend，连接到 Provisioner 服务

配置示例 (config.yaml):
    sandbox:
        use: deerflow.community.aio_sandbox:AioSandboxProvider
        image: <容器镜像>
        port: 8080
        container_prefix: deer-flow-sandbox
        idle_timeout: 600
        replicas: 3
        mounts:
          - host_path: /path/on/host
            container_path: /path/in/container
            read_only: false
        environment:
          NODE_ENV: production
          API_KEY: $MY_API_KEY
"""

import atexit
import hashlib
import logging
import os
import signal
import threading
import time
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]
    import msvcrt

from deerflow.config import get_app_config
from deerflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider

from .aio_sandbox import AioSandbox
from .backend import SandboxBackend, wait_for_sandbox_ready
from .local_backend import LocalContainerBackend
from .remote_backend import RemoteSandboxBackend
from .sandbox_info import SandboxInfo

logger = logging.getLogger(__name__)

# ── 默认配置常量 ──
DEFAULT_IMAGE = "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest"
DEFAULT_PORT = 8080
DEFAULT_CONTAINER_PREFIX = "deer-flow-sandbox"
DEFAULT_IDLE_TIMEOUT = 600  # 10 分钟（秒）
DEFAULT_REPLICAS = 3  # 最大并发沙箱容器数
IDLE_CHECK_INTERVAL = 60  # 空闲检查间隔（秒）


def _lock_file_exclusive(lock_file) -> None:
    """对文件施加排他锁（跨平台兼容）。

    在 Unix 系统上使用 fcntl.flock，在 Windows 上使用 msvcrt.locking。
    该锁用于序列化多进程间的沙箱创建操作。

    Args:
        lock_file: 已打开的文件对象。
    """
    if fcntl is not None:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        return

    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_file(lock_file) -> None:
    """释放文件的排他锁（跨平台兼容）。

    Args:
        lock_file: 已加锁的文件对象。
    """
    if fcntl is not None:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return

    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


class AioSandboxProvider(SandboxProvider):
    """管理 AIO 沙箱容器的 Provider 实现。

    架构设计:
        Provider 组合 SandboxBackend（沙箱的配置方式），支持：
        - 本地 Docker/Apple Container 模式（自动启动容器）
        - 远程/K8s 模式（连接到预存在的沙箱 URL）

    该 Provider 继承自 SandboxProvider 抽象基类，实现了沙箱的获取、
    释放、销毁和关闭等核心接口。

    配置选项在 config.yaml 的 sandbox 段下：
        use: deerflow.community.aio_sandbox:AioSandboxProvider
        image: <容器镜像>
        port: 8080                      # 本地容器的基础端口
        container_prefix: deer-flow-sandbox
        idle_timeout: 600               # 空闲超时秒数（0 表示禁用）
        replicas: 3                     # 最大并发沙箱容器数（超限时 LRU 淘汰）
        mounts:                         # 本地容器的卷挂载配置
          - host_path: /path/on/host
            container_path: /path/in/container
            read_only: false
        environment:                    # 容器环境变量
          NODE_ENV: production
          API_KEY: $MY_API_KEY
    """

    def __init__(self):
        """初始化沙箱 Provider。

        初始化内部状态（沙箱缓存、线程映射、暖池等），加载配置，
        创建后端实例，注册关闭处理器，协调孤儿容器，并启动空闲检查器。
        """
        self._lock = threading.Lock()
        self._sandboxes: dict[str, AioSandbox] = {}  # sandbox_id -> AioSandbox 实例
        self._sandbox_infos: dict[str, SandboxInfo] = {}  # sandbox_id -> SandboxInfo（用于销毁）
        self._thread_sandboxes: dict[str, str] = {}  # thread_id -> sandbox_id
        self._thread_locks: dict[str, threading.Lock] = {}  # thread_id -> 进程内锁
        self._last_activity: dict[str, float] = {}  # sandbox_id -> 最后活动时间戳
        # 暖池：已释放但容器仍在运行的沙箱。
        # 映射 sandbox_id -> (SandboxInfo, 释放时间戳)。
        # 暖池中的容器可以被快速回收（无需冷启动），或在 replicas 容量
        # 不足时被销毁以释放资源。
        self._warm_pool: dict[str, tuple[SandboxInfo, float]] = {}
        self._shutdown_called = False
        self._idle_checker_stop = threading.Event()
        self._idle_checker_thread: threading.Thread | None = None

        self._config = self._load_config()
        self._backend: SandboxBackend = self._create_backend()

        # 注册关闭处理器，确保进程退出时清理所有容器
        atexit.register(self.shutdown)
        self._register_signal_handlers()

        # 协调之前进程生命周期遗留的孤儿容器
        self._reconcile_orphans()

        # 如果启用了空闲超时，启动后台空闲检查线程
        if self._config.get("idle_timeout", DEFAULT_IDLE_TIMEOUT) > 0:
            self._start_idle_checker()

    @property
    def uses_thread_data_mounts(self) -> bool:
        """判断线程数据目录是否通过卷挂载可见。

        本地容器后端会绑定挂载线程数据目录，因此网关写入的文件在沙箱
        启动时就已经可见。远程后端可能需要显式的文件同步。

        Returns:
            如果使用本地容器后端返回 True，否则返回 False。
        """
        return isinstance(self._backend, LocalContainerBackend)

    # ── 工厂方法 ──────────────────────────────────────────────────────────

    def _create_backend(self) -> SandboxBackend:
        """根据配置创建适当的沙箱后端。

        选择逻辑（按优先级检查）：
        1. provisioner_url 已设置 → RemoteSandboxBackend（Provisioner 模式）
           Provisioner 在 k3s 中动态创建 Pod + Service。
        2. 默认 → LocalContainerBackend（本地模式）
           本地 Provider 直接管理容器生命周期（启动/停止）。

        Returns:
            配置好的 SandboxBackend 实例。
        """
        provisioner_url = self._config.get("provisioner_url")
        if provisioner_url:
            logger.info(f"Using remote sandbox backend with provisioner at {provisioner_url}")
            return RemoteSandboxBackend(provisioner_url=provisioner_url)

        logger.info("Using local container sandbox backend")
        return LocalContainerBackend(
            image=self._config["image"],
            base_port=self._config["port"],
            container_prefix=self._config["container_prefix"],
            config_mounts=self._config["mounts"],
            environment=self._config["environment"],
        )

    # ── 配置加载 ──────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        """从应用配置加载沙箱配置。

        合并用户配置和默认值，解析环境变量引用。

        Returns:
            包含完整沙箱配置的字典。
        """
        config = get_app_config()
        sandbox_config = config.sandbox

        idle_timeout = getattr(sandbox_config, "idle_timeout", None)
        replicas = getattr(sandbox_config, "replicas", None)

        return {
            "image": sandbox_config.image or DEFAULT_IMAGE,
            "port": sandbox_config.port or DEFAULT_PORT,
            "container_prefix": sandbox_config.container_prefix or DEFAULT_CONTAINER_PREFIX,
            "idle_timeout": idle_timeout if idle_timeout is not None else DEFAULT_IDLE_TIMEOUT,
            "replicas": replicas if replicas is not None else DEFAULT_REPLICAS,
            "mounts": sandbox_config.mounts or [],
            "environment": self._resolve_env_vars(sandbox_config.environment or {}),
            # 用于动态 Pod 管理的 provisioner URL（例如 http://provisioner:8002）
            "provisioner_url": getattr(sandbox_config, "provisioner_url", None) or "",
        }

    @staticmethod
    def _resolve_env_vars(env_config: dict[str, str]) -> dict[str, str]:
        """解析环境变量引用（以 $ 开头的值）。

        配置中形如 API_KEY: $MY_API_KEY 的条目会被解析为从系统环境变量
        中读取 MY_API_KEY 的值。

        Args:
            env_config: 原始环境变量配置字典。

        Returns:
            解析后的环境变量字典。
        """
        resolved = {}
        for key, value in env_config.items():
            if isinstance(value, str) and value.startswith("$"):
                env_name = value[1:]
                resolved[key] = os.environ.get(env_name, "")
            else:
                resolved[key] = str(value)
        return resolved

    # ── 启动时孤儿协调 ──────────────────────────────────────────────────

    def _reconcile_orphans(self) -> None:
        """协调之前进程生命周期遗留的孤儿容器。

        启动时，枚举所有匹配前缀的运行中容器，并将它们全部收养到暖池中。
        空闲检查器将回收那些没有被任何线程重新获取的容器。

        所有容器被无条件收养，因为我们无法仅凭年龄区分"孤儿"和"正被
        其他进程使用"— idle_timeout 表示不活跃时间，而非运行时间。
        收养到暖池并让空闲检查器决定，可以避免销毁并发进程可能仍在
        使用的容器。

        这填补了一个根本性缺口：进程内状态丢失（进程重启、崩溃、SIGKILL）
        会导致 Docker 容器永远运行下去。
        """
        try:
            running = self._backend.list_running()
        except Exception as e:
            logger.warning(f"Failed to enumerate running containers during startup reconciliation: {e}")
            return

        if not running:
            return

        current_time = time.time()
        adopted = 0

        for info in running:
            age = current_time - info.created_at if info.created_at > 0 else float("inf")
            # 每个容器单次锁获取：原子性的检查并插入。
            # 避免"已跟踪？"检查和暖池插入之间的 TOCTOU 竞态窗口。
            with self._lock:
                if info.sandbox_id in self._sandboxes or info.sandbox_id in self._warm_pool:
                    continue
                self._warm_pool[info.sandbox_id] = (info, current_time)
            adopted += 1
            logger.info(f"Adopted container {info.sandbox_id} into warm pool (age: {age:.0f}s)")

        logger.info(f"Startup reconciliation complete: {adopted} adopted into warm pool, {len(running)} total found")

    # ── 确定性 ID 生成 ──────────────────────────────────────────────────

    @staticmethod
    def _deterministic_sandbox_id(thread_id: str) -> str:
        """从线程 ID 生成确定性的沙箱 ID。

        确保所有进程为同一 thread_id 派生出相同的 sandbox_id，
        从而实现无需共享内存的跨进程沙箱发现。

        Args:
            thread_id: 线程的唯一标识符。

        Returns:
            8 字符的十六进制沙箱 ID。
        """
        return hashlib.sha256(thread_id.encode()).hexdigest()[:8]

    # ── 挂载点辅助方法 ──────────────────────────────────────────────────

    def _get_extra_mounts(self, thread_id: str | None) -> list[tuple[str, str, bool]]:
        """收集沙箱的所有额外挂载点（线程特定 + 技能目录）。

        Args:
            thread_id: 线程 ID，用于获取线程特定的数据目录挂载。

        Returns:
            挂载点元组列表，每个元组格式为 (host_path, container_path, read_only)。
        """
        mounts: list[tuple[str, str, bool]] = []

        if thread_id:
            mounts.extend(self._get_thread_mounts(thread_id))
            logger.info(f"Adding thread mounts for thread {thread_id}: {mounts}")

        skills_mount = self._get_skills_mount()
        if skills_mount:
            mounts.append(skills_mount)
            logger.info(f"Adding skills mount: {skills_mount}")

        return mounts

    @staticmethod
    def _get_thread_mounts(thread_id: str) -> list[tuple[str, str, bool]]:
        """获取线程数据目录的卷挂载配置。

        如果目录不存在则自动创建（惰性初始化）。挂载源使用 host_base_dir，
        以便在 Docker-outside-of-Docker（DooD）模式下，宿主机的 Docker
        守护进程能够正确解析路径。

        Args:
            thread_id: 线程的唯一标识符。

        Returns:
            挂载点元组列表：(宿主机路径, 容器内路径, 是否只读)。
        """
        paths = get_paths()
        user_id = get_effective_user_id()
        paths.ensure_thread_dirs(thread_id, user_id=user_id)

        return [
            # 工作区目录：可读写，沙箱内代码在此执行
            (paths.host_sandbox_work_dir(thread_id, user_id=user_id), f"{VIRTUAL_PATH_PREFIX}/workspace", False),
            # 上传目录：可读写，用户上传的文件存放位置
            (paths.host_sandbox_uploads_dir(thread_id, user_id=user_id), f"{VIRTUAL_PATH_PREFIX}/uploads", False),
            # 输出目录：可读写，沙箱生成的结果文件存放位置
            (paths.host_sandbox_outputs_dir(thread_id, user_id=user_id), f"{VIRTUAL_PATH_PREFIX}/outputs", False),
            # ACP 工作区：只读挂载到沙箱内（主代理读取结果；
            # ACP 子进程从宿主侧写入，而非从容器内部写入）
            (paths.host_acp_workspace_dir(thread_id, user_id=user_id), "/mnt/acp-workspace", True),
        ]

    @staticmethod
    def _get_skills_mount() -> tuple[str, str, bool] | None:
        """获取技能目录的挂载配置。

        当运行在 Docker（DooD）模式下时，使用 DEER_FLOW_HOST_SKILLS_PATH
        环境变量指向的宿主机路径，以便宿主机的 Docker 守护进程能解析路径。

        Returns:
            挂载点元组 (host_path, container_path, True) 或 None（如果技能目录不存在）。
        """
        try:
            config = get_app_config()
            skills_path = config.skills.get_skills_path()
            container_path = config.skills.container_path

            if skills_path.exists():
                # 在 Docker DooD 模式下使用宿主侧技能路径
                host_skills = os.environ.get("DEER_FLOW_HOST_SKILLS_PATH") or str(skills_path)
                return (host_skills, container_path, True)  # 只读挂载以确保安全
        except Exception as e:
            logger.warning(f"Could not setup skills mount: {e}")
        return None

    # ── 空闲超时管理 ──────────────────────────────────────────────────

    def _start_idle_checker(self) -> None:
        """启动后台空闲检查线程。

        该线程定期扫描所有活跃沙箱和暖池条目，销毁超过空闲超时的沙箱。
        """
        self._idle_checker_thread = threading.Thread(
            target=self._idle_checker_loop,
            name="sandbox-idle-checker",
            daemon=True,
        )
        self._idle_checker_thread.start()
        logger.info(f"Started idle checker thread (timeout: {self._config.get('idle_timeout', DEFAULT_IDLE_TIMEOUT)}s)")

    def _idle_checker_loop(self) -> None:
        """空闲检查线程的主循环。

        每隔 IDLE_CHECK_INTERVAL 秒执行一次空闲沙箱清理。
        """
        idle_timeout = self._config.get("idle_timeout", DEFAULT_IDLE_TIMEOUT)
        while not self._idle_checker_stop.wait(timeout=IDLE_CHECK_INTERVAL):
            try:
                self._cleanup_idle_sandboxes(idle_timeout)
            except Exception as e:
                logger.error(f"Error in idle checker loop: {e}")

    def _cleanup_idle_sandboxes(self, idle_timeout: float) -> None:
        """清理所有超过空闲超时的沙箱。

        分别检查活跃沙箱（通过 _last_activity 跟踪）和暖池条目
        （通过释放时间戳跟踪）。活跃沙箱在销毁前会重新验证是否
        仍然空闲，以避免竞态条件。

        Args:
            idle_timeout: 空闲超时阈值（秒）。
        """
        current_time = time.time()
        active_to_destroy = []
        warm_to_destroy: list[tuple[str, SandboxInfo]] = []

        with self._lock:
            # 活跃沙箱：通过 _last_activity 跟踪空闲时间
            for sandbox_id, last_activity in self._last_activity.items():
                idle_duration = current_time - last_activity
                if idle_duration > idle_timeout:
                    active_to_destroy.append(sandbox_id)
                    logger.info(f"Sandbox {sandbox_id} idle for {idle_duration:.1f}s, marking for destroy")

            # 暖池：通过存储在 _warm_pool 中的释放时间戳跟踪
            for sandbox_id, (info, release_ts) in list(self._warm_pool.items()):
                warm_duration = current_time - release_ts
                if warm_duration > idle_timeout:
                    warm_to_destroy.append((sandbox_id, info))
                    del self._warm_pool[sandbox_id]
                    logger.info(f"Warm-pool sandbox {sandbox_id} idle for {warm_duration:.1f}s, marking for destroy")

        # 销毁活跃沙箱（操作前重新验证是否仍然空闲）
        for sandbox_id in active_to_destroy:
            try:
                # 在加锁后重新验证沙箱是否仍然空闲。
                # 在快照和此处之间，沙箱可能已被重新获取（last_activity 已更新）
                # 或已被释放/销毁。
                with self._lock:
                    last_activity = self._last_activity.get(sandbox_id)
                    if last_activity is None:
                        # 已被其他路径释放或销毁 — 跳过
                        logger.info(f"Sandbox {sandbox_id} already gone before idle destroy, skipping")
                        continue
                    if (time.time() - last_activity) < idle_timeout:
                        # 自快照以来已被重新获取（活动时间已更新）— 跳过
                        logger.info(f"Sandbox {sandbox_id} was re-acquired before idle destroy, skipping")
                        continue
                logger.info(f"Destroying idle sandbox {sandbox_id}")
                self.destroy(sandbox_id)
            except Exception as e:
                logger.error(f"Failed to destroy idle sandbox {sandbox_id}: {e}")

        # 销毁暖池沙箱（已在上面加锁时从 _warm_pool 中移除）
        for sandbox_id, info in warm_to_destroy:
            try:
                self._backend.destroy(info)
                logger.info(f"Destroyed idle warm-pool sandbox {sandbox_id}")
            except Exception as e:
                logger.error(f"Failed to destroy idle warm-pool sandbox {sandbox_id}: {e}")

    # ── 信号处理 ──────────────────────────────────────────────────────────

    def _register_signal_handlers(self) -> None:
        """注册信号处理器以实现优雅关闭。

        处理 SIGTERM、SIGINT 和 SIGHUP（终端关闭）信号，确保即使用户
        关闭终端，沙箱容器也能被正确清理。
        """
        self._original_sigterm = signal.getsignal(signal.SIGTERM)
        self._original_sigint = signal.getsignal(signal.SIGINT)
        self._original_sighup = signal.getsignal(signal.SIGHUP) if hasattr(signal, "SIGHUP") else None

        def signal_handler(signum, frame):
            self.shutdown()
            if signum == signal.SIGTERM:
                original = self._original_sigterm
            elif hasattr(signal, "SIGHUP") and signum == signal.SIGHUP:
                original = self._original_sighup
            else:
                original = self._original_sigint
            if callable(original):
                original(signum, frame)
            elif original == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)

        try:
            signal.signal(signal.SIGTERM, signal_handler)
            signal.signal(signal.SIGINT, signal_handler)
            if hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP, signal_handler)
        except ValueError:
            # 非主线程无法注册信号处理器
            logger.debug("Could not register signal handlers (not main thread)")

    # ── 线程锁管理（进程内） ──────────────────────────────────────────────

    def _get_thread_lock(self, thread_id: str) -> threading.Lock:
        """获取或创建特定 thread_id 的进程内锁。

        确保同一进程内对同一线程的沙箱操作是串行化的。

        Args:
            thread_id: 线程的唯一标识符。

        Returns:
            该线程对应的线程锁。
        """
        with self._lock:
            if thread_id not in self._thread_locks:
                self._thread_locks[thread_id] = threading.Lock()
            return self._thread_locks[thread_id]

    # ── 核心操作：acquire / get / release / shutdown ─────────────────────

    def acquire(self, thread_id: str | None = None) -> str:
        """获取沙箱环境并返回其 ID。

        对于同一个 thread_id，此方法会在多个轮次、多个进程间返回
        相同的 sandbox_id（在共享存储的 K8s 环境下跨 Pod 也是如此）。

        线程安全性通过进程内锁和跨进程文件锁两层保障。

        Args:
            thread_id: 可选的线程 ID，用于线程特定的配置。

        Returns:
            获取到的沙箱环境 ID。
        """
        if thread_id:
            thread_lock = self._get_thread_lock(thread_id)
            with thread_lock:
                return self._acquire_internal(thread_id)
        else:
            return self._acquire_internal(thread_id)

    def _acquire_internal(self, thread_id: str | None) -> str:
        """内部沙箱获取逻辑，实现两层一致性。

        Layer 1: 进程内缓存 — 最快路径，覆盖同一进程内的重复访问
        Layer 1.5: 暖池 — 容器仍在运行，无需冷启动
        Layer 2: 后端发现 + 创建 — 覆盖其他进程启动的容器；
                 sandbox_id 从 thread_id 确定性派生，无需共享状态文件，
                 任何进程都能推导出相同的容器名

        Args:
            thread_id: 可选的线程 ID。

        Returns:
            获取到的沙箱 ID。
        """
        # ── Layer 1: 进程内缓存（快速路径） ──
        if thread_id:
            with self._lock:
                if thread_id in self._thread_sandboxes:
                    existing_id = self._thread_sandboxes[thread_id]
                    if existing_id in self._sandboxes:
                        logger.info(f"Reusing in-process sandbox {existing_id} for thread {thread_id}")
                        self._last_activity[existing_id] = time.time()
                        return existing_id
                    else:
                        # 缓存过期：sandbox_id 映射存在但沙箱实例已被移除
                        del self._thread_sandboxes[thread_id]

        # 为线程特定请求生成确定性 ID，匿名请求使用随机 ID
        sandbox_id = self._deterministic_sandbox_id(thread_id) if thread_id else str(uuid.uuid4())[:8]

        # ── Layer 1.5: 暖池（容器仍在运行，无需冷启动） ──
        if thread_id:
            with self._lock:
                if sandbox_id in self._warm_pool:
                    info, _ = self._warm_pool.pop(sandbox_id)
                    sandbox = AioSandbox(id=sandbox_id, base_url=info.sandbox_url)
                    self._sandboxes[sandbox_id] = sandbox
                    self._sandbox_infos[sandbox_id] = info
                    self._last_activity[sandbox_id] = time.time()
                    self._thread_sandboxes[thread_id] = sandbox_id
                    logger.info(f"Reclaimed warm-pool sandbox {sandbox_id} for thread {thread_id} at {info.sandbox_url}")
                    return sandbox_id

        # ── Layer 2: 后端发现 + 创建（受跨进程锁保护） ──
        # 使用文件锁使竞争创建同一沙箱的两个进程在此处串行化：
        # 第二个进程将发现第一个进程已启动的容器，而非触发名称冲突。
        if thread_id:
            return self._discover_or_create_with_lock(thread_id, sandbox_id)

        return self._create_sandbox(thread_id, sandbox_id)

    def _discover_or_create_with_lock(self, thread_id: str, sandbox_id: str) -> str:
        """在跨进程文件锁保护下发现现有沙箱或创建新沙箱。

        文件锁串行化多进程间对同一 thread_id 的并发沙箱创建操作，
        防止容器名称冲突。

        Args:
            thread_id: 线程的唯一标识符。
            sandbox_id: 确定性的沙箱 ID。

        Returns:
            沙箱 ID。
        """
        paths = get_paths()
        user_id = get_effective_user_id()
        paths.ensure_thread_dirs(thread_id, user_id=user_id)
        lock_path = paths.thread_dir(thread_id, user_id=user_id) / f"{sandbox_id}.lock"

        with open(lock_path, "a", encoding="utf-8") as lock_file:
            locked = False
            try:
                _lock_file_exclusive(lock_file)
                locked = True
                # 在文件锁下重新检查进程内缓存，防止本进程中的另一个
                # 线程在等待期间赢得了竞争
                with self._lock:
                    if thread_id in self._thread_sandboxes:
                        existing_id = self._thread_sandboxes[thread_id]
                        if existing_id in self._sandboxes:
                            logger.info(f"Reusing in-process sandbox {existing_id} for thread {thread_id} (post-lock check)")
                            self._last_activity[existing_id] = time.time()
                            return existing_id
                    if sandbox_id in self._warm_pool:
                        info, _ = self._warm_pool.pop(sandbox_id)
                        sandbox = AioSandbox(id=sandbox_id, base_url=info.sandbox_url)
                        self._sandboxes[sandbox_id] = sandbox
                        self._sandbox_infos[sandbox_id] = info
                        self._last_activity[sandbox_id] = time.time()
                        self._thread_sandboxes[thread_id] = sandbox_id
                        logger.info(f"Reclaimed warm-pool sandbox {sandbox_id} for thread {thread_id} (post-lock check)")
                        return sandbox_id

                # 后端发现：另一个进程可能已经创建了容器
                discovered = self._backend.discover(sandbox_id)
                if discovered is not None:
                    sandbox = AioSandbox(id=discovered.sandbox_id, base_url=discovered.sandbox_url)
                    with self._lock:
                        self._sandboxes[discovered.sandbox_id] = sandbox
                        self._sandbox_infos[discovered.sandbox_id] = discovered
                        self._last_activity[discovered.sandbox_id] = time.time()
                        self._thread_sandboxes[thread_id] = discovered.sandbox_id
                    logger.info(f"Discovered existing sandbox {discovered.sandbox_id} for thread {thread_id} at {discovered.sandbox_url}")
                    return discovered.sandbox_id

                # 发现失败，创建新沙箱
                return self._create_sandbox(thread_id, sandbox_id)
            finally:
                if locked:
                    _unlock_file(lock_file)

    def _evict_oldest_warm(self) -> str | None:
        """销毁暖池中最早的容器以释放容量。

        使用 LRU（最近最少使用）策略选择淘汰目标。

        Returns:
            被淘汰的 sandbox_id，如果暖池为空则返回 None。
        """
        with self._lock:
            if not self._warm_pool:
                return None
            # 选择释放时间戳最小（最早释放）的沙箱进行淘汰
            oldest_id = min(self._warm_pool, key=lambda sid: self._warm_pool[sid][1])
            info, _ = self._warm_pool.pop(oldest_id)

        try:
            self._backend.destroy(info)
            logger.info(f"Destroyed warm-pool sandbox {oldest_id}")
        except Exception as e:
            logger.error(f"Failed to destroy warm-pool sandbox {oldest_id}: {e}")
            return None
        return oldest_id

    def _create_sandbox(self, thread_id: str | None, sandbox_id: str) -> str:
        """通过后端创建新的沙箱。

        执行 replicas 容量检查（仅暖池容器计入淘汰预算），等待沙箱
        就绪后将沙箱实例注册到内部缓存中。

        Args:
            thread_id: 可选的线程 ID。
            sandbox_id: 要使用的沙箱 ID。

        Returns:
            沙箱 ID。

        Raises:
            RuntimeError: 如果沙箱创建失败或就绪检查超时。
        """
        extra_mounts = self._get_extra_mounts(thread_id)

        # 执行 replicas 限制：仅暖池容器计入淘汰预算。
        # 活跃沙箱正被活动线程使用，不得强制停止。
        replicas = self._config.get("replicas", DEFAULT_REPLICAS)
        with self._lock:
            total = len(self._sandboxes) + len(self._warm_pool)
        if total >= replicas:
            evicted = self._evict_oldest_warm()
            if evicted:
                logger.info(f"Evicted warm-pool sandbox {evicted} to stay within replicas={replicas}")
            else:
                # 所有槽位都被活跃沙箱占用 — 继续创建并记录警告。
                # replicas 限制是软上限；我们从不强制停止正在服务线程的容器。
                logger.warning(f"All {replicas} replica slots are in active use; creating sandbox {sandbox_id} beyond the soft limit")

        info = self._backend.create(thread_id, sandbox_id, extra_mounts=extra_mounts or None)

        # 等待沙箱就绪
        if not wait_for_sandbox_ready(info.sandbox_url, timeout=60):
            self._backend.destroy(info)
            raise RuntimeError(f"Sandbox {sandbox_id} failed to become ready within timeout at {info.sandbox_url}")

        sandbox = AioSandbox(id=sandbox_id, base_url=info.sandbox_url)
        with self._lock:
            self._sandboxes[sandbox_id] = sandbox
            self._sandbox_infos[sandbox_id] = info
            self._last_activity[sandbox_id] = time.time()
            if thread_id:
                self._thread_sandboxes[thread_id] = sandbox_id

        logger.info(f"Created sandbox {sandbox_id} for thread {thread_id} at {info.sandbox_url}")
        return sandbox_id

    def get(self, sandbox_id: str) -> Sandbox | None:
        """根据 ID 获取沙箱实例，并更新最后活动时间戳。

        Args:
            sandbox_id: 沙箱的唯一标识符。

        Returns:
            如果找到则返回沙箱实例，否则返回 None。
        """
        with self._lock:
            sandbox = self._sandboxes.get(sandbox_id)
            if sandbox is not None:
                self._last_activity[sandbox_id] = time.time()
            return sandbox

    def release(self, sandbox_id: str) -> None:
        """将沙箱从活跃使用中释放到暖池。

        容器继续运行，以便同一线程在下一轮次可以快速回收而无需冷启动。
        容器仅在 replicas 限制强制淘汰或系统关闭时才会被停止。

        Args:
            sandbox_id: 要释放的沙箱 ID。
        """
        info = None
        thread_ids_to_remove: list[str] = []

        with self._lock:
            self._sandboxes.pop(sandbox_id, None)
            info = self._sandbox_infos.pop(sandbox_id, None)
            # 清理该沙箱关联的所有线程映射
            thread_ids_to_remove = [tid for tid, sid in self._thread_sandboxes.items() if sid == sandbox_id]
            for tid in thread_ids_to_remove:
                del self._thread_sandboxes[tid]
            self._last_activity.pop(sandbox_id, None)
            # 停放到暖池中 — 容器继续运行
            if info and sandbox_id not in self._warm_pool:
                self._warm_pool[sandbox_id] = (info, time.time())

        logger.info(f"Released sandbox {sandbox_id} to warm pool (container still running)")

    def destroy(self, sandbox_id: str) -> None:
        """销毁沙箱：停止容器并释放所有资源。

        与 release() 不同，此方法会实际停止容器。用于显式清理、
        容量驱动的淘汰或系统关闭。

        Args:
            sandbox_id: 要销毁的沙箱 ID。
        """
        info = None
        thread_ids_to_remove: list[str] = []

        with self._lock:
            self._sandboxes.pop(sandbox_id, None)
            info = self._sandbox_infos.pop(sandbox_id, None)
            # 清理该沙箱关联的所有线程映射
            thread_ids_to_remove = [tid for tid, sid in self._thread_sandboxes.items() if sid == sandbox_id]
            for tid in thread_ids_to_remove:
                del self._thread_sandboxes[tid]
            self._last_activity.pop(sandbox_id, None)
            # 如果沙箱在暖池中，也要取出
            if info is None and sandbox_id in self._warm_pool:
                info, _ = self._warm_pool.pop(sandbox_id)
            else:
                self._warm_pool.pop(sandbox_id, None)

        if info:
            self._backend.destroy(info)
            logger.info(f"Destroyed sandbox {sandbox_id}")

    def shutdown(self) -> None:
        """关闭所有沙箱。线程安全且幂等。

        按顺序停止空闲检查器线程，销毁所有活跃沙箱和暖池中的容器。
        通过 _shutdown_called 标志确保多次调用不会重复执行。
        """
        with self._lock:
            if self._shutdown_called:
                return
            self._shutdown_called = True
            sandbox_ids = list(self._sandboxes.keys())
            warm_items = list(self._warm_pool.items())
            self._warm_pool.clear()

        # 停止空闲检查器
        self._idle_checker_stop.set()
        if self._idle_checker_thread is not None and self._idle_checker_thread.is_alive():
            self._idle_checker_thread.join(timeout=5)
            logger.info("Stopped idle checker thread")

        logger.info(f"Shutting down {len(sandbox_ids)} active + {len(warm_items)} warm-pool sandbox(es)")

        # 销毁所有活跃沙箱
        for sandbox_id in sandbox_ids:
            try:
                self.destroy(sandbox_id)
            except Exception as e:
                logger.error(f"Failed to destroy sandbox {sandbox_id} during shutdown: {e}")

        # 销毁所有暖池沙箱
        for sandbox_id, (info, _) in warm_items:
            try:
                self._backend.destroy(info)
                logger.info(f"Destroyed warm-pool sandbox {sandbox_id} during shutdown")
            except Exception as e:
                logger.error(f"Failed to destroy warm-pool sandbox {sandbox_id} during shutdown: {e}")
