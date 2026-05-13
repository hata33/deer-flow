"""AIO 沙箱提供者。

编排沙箱生命周期，支持可插拔后端（本地容器 vs 远程 K8s）。
核心特性：
- 进程内缓存加速重复访问
- 温池（warm pool）避免冷启动
- 空闲超时自动回收
- 信号处理优雅关闭
- 挂载计算（线程目录、skills）
- 跨进程文件锁防止并发冲突
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
except ImportError:  # Windows 回退
    fcntl = None  # type: ignore[assignment]
    import msvcrt

from deerflow.config import get_app_config
from deerflow.config.paths import VIRTUAL_PATH_PREFIX, Paths, get_paths
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider

from .aio_sandbox import AioSandbox
from .backend import SandboxBackend, wait_for_sandbox_ready
from .local_backend import LocalContainerBackend
from .remote_backend import RemoteSandboxBackend
from .sandbox_info import SandboxInfo

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_IMAGE = "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest"
DEFAULT_PORT = 8080
DEFAULT_CONTAINER_PREFIX = "deer-flow-sandbox"
DEFAULT_IDLE_TIMEOUT = 600  # 10 分钟
DEFAULT_REPLICAS = 3  # 最大并发沙箱容器数
IDLE_CHECK_INTERVAL = 60  # 空闲检查间隔


def _lock_file_exclusive(lock_file) -> None:
    """跨平台文件独占锁（Unix: fcntl, Windows: msvcrt）。"""
    if fcntl is not None:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        return

    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_file(lock_file) -> None:
    """释放文件锁。"""
    if fcntl is not None:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return

    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


class AioSandboxProvider(SandboxProvider):
    """基于 Docker 容器的沙箱提供者。

    架构：组合 SandboxBackend（如何分配），支持：
    - 本地 Docker/Apple Container 模式（自动启动容器）
    - 远程/K8s 模式（连接预存沙箱 URL）

    配置示例（config.yaml sandbox 段）::
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

    def __init__(self):
        self._lock = threading.Lock()
        self._sandboxes: dict[str, AioSandbox] = {}  # sandbox_id → AioSandbox 实例
        self._sandbox_infos: dict[str, SandboxInfo] = {}  # sandbox_id → SandboxInfo（用于销毁）
        self._thread_sandboxes: dict[str, str] = {}  # thread_id → sandbox_id
        self._thread_locks: dict[str, threading.Lock] = {}  # thread_id → 进程内锁
        self._last_activity: dict[str, float] = {}  # sandbox_id → 最后活动时间戳
        # 温池：已释放但容器仍运行的沙箱，可快速回收避免冷启动
        self._warm_pool: dict[str, tuple[SandboxInfo, float]] = {}
        self._shutdown_called = False
        self._idle_checker_stop = threading.Event()
        self._idle_checker_thread: threading.Thread | None = None

        self._config = self._load_config()
        self._backend: SandboxBackend = self._create_backend()

        # 注册关闭钩子
        atexit.register(self.shutdown)
        self._register_signal_handlers()

        # 启动空闲检查线程
        if self._config.get("idle_timeout", DEFAULT_IDLE_TIMEOUT) > 0:
            self._start_idle_checker()

    # ── 工厂方法 ──────────────────────────────────────────────────

    def _create_backend(self) -> SandboxBackend:
        """根据配置创建后端：有 provisioner_url → 远程，否则 → 本地容器。"""
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

    # ── 配置加载 ──────────────────────────────────────────────────

    def _load_config(self) -> dict:
        """从应用配置加载沙箱参数。"""
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
            "provisioner_url": getattr(sandbox_config, "provisioner_url", None) or "",
        }

    @staticmethod
    def _resolve_env_vars(env_config: dict[str, str]) -> dict[str, str]:
        """解析环境变量引用（$ 前缀）。"""
        resolved = {}
        for key, value in env_config.items():
            if isinstance(value, str) and value.startswith("$"):
                env_name = value[1:]
                resolved[key] = os.environ.get(env_name, "")
            else:
                resolved[key] = str(value)
        return resolved

    # ── 确定性 ID ─────────────────────────────────────────────────

    @staticmethod
    def _deterministic_sandbox_id(thread_id: str) -> str:
        """从 thread_id 生成确定性沙箱 ID（SHA256 前 8 位）。

        所有进程对同一 thread_id 推导出相同 sandbox_id，实现无共享状态的跨进程发现。
        """
        return hashlib.sha256(thread_id.encode()).hexdigest()[:8]

    # ── 挂载辅助 ──────────────────────────────────────────────────

    def _get_extra_mounts(self, thread_id: str | None) -> list[tuple[str, str, bool]]:
        """收集沙箱的所有额外挂载（线程目录 + skills）。"""
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
        """获取线程数据目录的卷挂载（按需创建目录）。

        使用 host_base_dir 确保在 Docker-in-Docker 场景下宿主机 Docker 守护进程能解析路径。
        """
        paths = get_paths()
        paths.ensure_thread_dirs(thread_id)

        # host_paths 在 DEER_FLOW_HOST_BASE_DIR 设置时解析为宿主机侧路径
        host_paths = Paths(base_dir=paths.host_base_dir)

        return [
            (str(host_paths.sandbox_work_dir(thread_id)), f"{VIRTUAL_PATH_PREFIX}/workspace", False),
            (str(host_paths.sandbox_uploads_dir(thread_id)), f"{VIRTUAL_PATH_PREFIX}/uploads", False),
            (str(host_paths.sandbox_outputs_dir(thread_id)), f"{VIRTUAL_PATH_PREFIX}/outputs", False),
            # ACP 工作区：沙箱内只读（lead agent 读取结果，ACP 子进程从宿主机侧写入）
            (str(host_paths.acp_workspace_dir(thread_id)), "/mnt/acp-workspace", True),
        ]

    @staticmethod
    def _get_skills_mount() -> tuple[str, str, bool] | None:
        """获取 skills 目录挂载配置（只读）。"""
        try:
            config = get_app_config()
            skills_path = config.skills.get_skills_path()
            container_path = config.skills.container_path

            if skills_path.exists():
                # Docker-in-Docker 场景使用宿主机侧 skills 路径
                host_skills = os.environ.get("DEER_FLOW_HOST_SKILLS_PATH") or str(skills_path)
                return (host_skills, container_path, True)
        except Exception as e:
            logger.warning(f"Could not setup skills mount: {e}")
        return None

    # ── 空闲超时管理 ──────────────────────────────────────────────────

    def _start_idle_checker(self) -> None:
        """启动空闲检查后台线程。"""
        self._idle_checker_thread = threading.Thread(
            target=self._idle_checker_loop,
            name="sandbox-idle-checker",
            daemon=True,
        )
        self._idle_checker_thread.start()
        logger.info(f"Started idle checker thread (timeout: {self._config.get('idle_timeout', DEFAULT_IDLE_TIMEOUT)}s)")

    def _idle_checker_loop(self) -> None:
        """空闲检查循环：定期扫描并清理超时沙箱。"""
        idle_timeout = self._config.get("idle_timeout", DEFAULT_IDLE_TIMEOUT)
        while not self._idle_checker_stop.wait(timeout=IDLE_CHECK_INTERVAL):
            try:
                self._cleanup_idle_sandboxes(idle_timeout)
            except Exception as e:
                logger.error(f"Error in idle checker loop: {e}")

    def _cleanup_idle_sandboxes(self, idle_timeout: float) -> None:
        """清理活跃和温池中的空闲沙箱。

        销毁前重新验证空闲状态，避免竞态条件下误销毁刚被重新获取的沙箱。
        """
        current_time = time.time()
        active_to_destroy = []
        warm_to_destroy: list[tuple[str, SandboxInfo]] = []

        with self._lock:
            # 活跃沙箱：通过 _last_activity 跟踪
            for sandbox_id, last_activity in self._last_activity.items():
                idle_duration = current_time - last_activity
                if idle_duration > idle_timeout:
                    active_to_destroy.append(sandbox_id)
                    logger.info(f"Sandbox {sandbox_id} idle for {idle_duration:.1f}s, marking for destroy")

            # 温池：通过释放时间戳跟踪
            for sandbox_id, (info, release_ts) in list(self._warm_pool.items()):
                warm_duration = current_time - release_ts
                if warm_duration > idle_timeout:
                    warm_to_destroy.append((sandbox_id, info))
                    del self._warm_pool[sandbox_id]
                    logger.info(f"Warm-pool sandbox {sandbox_id} idle for {warm_duration:.1f}s, marking for destroy")

        # 销毁活跃沙箱（先重新验证空闲状态）
        for sandbox_id in active_to_destroy:
            try:
                with self._lock:
                    last_activity = self._last_activity.get(sandbox_id)
                    if last_activity is None:
                        logger.info(f"Sandbox {sandbox_id} already gone before idle destroy, skipping")
                        continue
                    if (time.time() - last_activity) < idle_timeout:
                        logger.info(f"Sandbox {sandbox_id} was re-acquired before idle destroy, skipping")
                        continue
                logger.info(f"Destroying idle sandbox {sandbox_id}")
                self.destroy(sandbox_id)
            except Exception as e:
                logger.error(f"Failed to destroy idle sandbox {sandbox_id}: {e}")

        # 销毁温池沙箱（已在锁内移除）
        for sandbox_id, info in warm_to_destroy:
            try:
                self._backend.destroy(info)
                logger.info(f"Destroyed idle warm-pool sandbox {sandbox_id}")
            except Exception as e:
                logger.error(f"Failed to destroy idle warm-pool sandbox {sandbox_id}: {e}")

    # ── 信号处理 ──────────────────────────────────────────────────

    def _register_signal_handlers(self) -> None:
        """注册 SIGTERM/SIGINT 处理器实现优雅关闭。"""
        self._original_sigterm = signal.getsignal(signal.SIGTERM)
        self._original_sigint = signal.getsignal(signal.SIGINT)

        def signal_handler(signum, frame):
            self.shutdown()
            original = self._original_sigterm if signum == signal.SIGTERM else self._original_sigint
            if callable(original):
                original(signum, frame)
            elif original == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)

        try:
            signal.signal(signal.SIGTERM, signal_handler)
            signal.signal(signal.SIGINT, signal_handler)
        except ValueError:
            logger.debug("Could not register signal handlers (not main thread)")

    # ── 线程锁（进程内） ──────────────────────────────────────────────

    def _get_thread_lock(self, thread_id: str) -> threading.Lock:
        """获取或创建线程级进程内锁。"""
        with self._lock:
            if thread_id not in self._thread_locks:
                self._thread_locks[thread_id] = threading.Lock()
            return self._thread_locks[thread_id]

    # ── 核心：acquire / get / release / shutdown ─────────────────────────

    def acquire(self, thread_id: str | None = None) -> str:
        """获取沙箱环境并返回其 ID。

        同一 thread_id 在多轮、多进程间返回相同 sandbox_id。
        线程安全：进程内锁 + 跨进程文件锁双重保护。
        """
        if thread_id:
            thread_lock = self._get_thread_lock(thread_id)
            with thread_lock:
                return self._acquire_internal(thread_id)
        else:
            return self._acquire_internal(thread_id)

    def _acquire_internal(self, thread_id: str | None) -> str:
        """内部获取逻辑：三层缓存查找。

        Layer 1: 进程内缓存（最快，覆盖同进程重复访问）
        Layer 1.5: 温池（容器仍在运行，无冷启动）
        Layer 2: 后端发现 + 创建（跨进程文件锁保护）
        """
        # ── Layer 1: 进程内缓存 ──
        if thread_id:
            with self._lock:
                if thread_id in self._thread_sandboxes:
                    existing_id = self._thread_sandboxes[thread_id]
                    if existing_id in self._sandboxes:
                        logger.info(f"Reusing in-process sandbox {existing_id} for thread {thread_id}")
                        self._last_activity[existing_id] = time.time()
                        return existing_id
                    else:
                        del self._thread_sandboxes[thread_id]

        # 确定性 ID（线程关联）或随机 ID（匿名）
        sandbox_id = self._deterministic_sandbox_id(thread_id) if thread_id else str(uuid.uuid4())[:8]

        # ── Layer 1.5: 温池回收 ──
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

        # ── Layer 2: 后端发现 + 创建（跨进程文件锁保护） ──
        if thread_id:
            return self._discover_or_create_with_lock(thread_id, sandbox_id)

        return self._create_sandbox(thread_id, sandbox_id)

    def _discover_or_create_with_lock(self, thread_id: str, sandbox_id: str) -> str:
        """跨进程文件锁保护下的发现或创建。

        文件锁序列化同一 thread_id 的并发创建，防止容器名冲突。
        """
        paths = get_paths()
        paths.ensure_thread_dirs(thread_id)
        lock_path = paths.thread_dir(thread_id) / f"{sandbox_id}.lock"

        with open(lock_path, "a", encoding="utf-8") as lock_file:
            locked = False
            try:
                _lock_file_exclusive(lock_file)
                locked = True
                # 获取锁后重新检查进程内缓存（可能被其他线程填充）
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

                # 后端发现：其他进程可能已创建容器
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

                return self._create_sandbox(thread_id, sandbox_id)
            finally:
                if locked:
                    _unlock_file(lock_file)

    def _evict_oldest_warm(self) -> str | None:
        """驱逐温池中最旧的容器以释放容量。"""
        with self._lock:
            if not self._warm_pool:
                return None
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
        """通过后端创建新沙箱。

        replicas 限制仅作用于温池驱逐，不强制停止活跃沙箱。
        """
        extra_mounts = self._get_extra_mounts(thread_id)

        # 副本限制：超过时驱逐温池中最旧的容器
        replicas = self._config.get("replicas", DEFAULT_REPLICAS)
        with self._lock:
            total = len(self._sandboxes) + len(self._warm_pool)
        if total >= replicas:
            evicted = self._evict_oldest_warm()
            if evicted:
                logger.info(f"Evicted warm-pool sandbox {evicted} to stay within replicas={replicas}")
            else:
                # 所有槽位被活跃沙箱占用，软限制，仍允许创建
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
        """根据 ID 获取沙箱实例，同时更新活动时间戳。"""
        with self._lock:
            sandbox = self._sandboxes.get(sandbox_id)
            if sandbox is not None:
                self._last_activity[sandbox_id] = time.time()
            return sandbox

    def release(self, sandbox_id: str) -> None:
        """将沙箱从活跃状态释放到温池。

        容器保持运行以便快速回收，仅在 replicas 限制驱逐或关闭时停止。
        """
        info = None
        thread_ids_to_remove: list[str] = []

        with self._lock:
            self._sandboxes.pop(sandbox_id, None)
            info = self._sandbox_infos.pop(sandbox_id, None)
            thread_ids_to_remove = [tid for tid, sid in self._thread_sandboxes.items() if sid == sandbox_id]
            for tid in thread_ids_to_remove:
                del self._thread_sandboxes[tid]
            self._last_activity.pop(sandbox_id, None)
            # 停入温池，容器继续运行
            if info and sandbox_id not in self._warm_pool:
                self._warm_pool[sandbox_id] = (info, time.time())

        logger.info(f"Released sandbox {sandbox_id} to warm pool (container still running)")

    def destroy(self, sandbox_id: str) -> None:
        """彻底销毁沙箱：停止容器并释放所有资源。

        与 release() 不同，此方法实际停止容器。
        """
        info = None
        thread_ids_to_remove: list[str] = []

        with self._lock:
            self._sandboxes.pop(sandbox_id, None)
            info = self._sandbox_infos.pop(sandbox_id, None)
            thread_ids_to_remove = [tid for tid, sid in self._thread_sandboxes.items() if sid == sandbox_id]
            for tid in thread_ids_to_remove:
                del self._thread_sandboxes[tid]
            self._last_activity.pop(sandbox_id, None)
            # 同时从温池中移除（如果在那里）
            if info is None and sandbox_id in self._warm_pool:
                info, _ = self._warm_pool.pop(sandbox_id)
            else:
                self._warm_pool.pop(sandbox_id, None)

        if info:
            self._backend.destroy(info)
            logger.info(f"Destroyed sandbox {sandbox_id}")

    def shutdown(self) -> None:
        """关闭所有沙箱。线程安全且幂等。"""
        with self._lock:
            if self._shutdown_called:
                return
            self._shutdown_called = True
            sandbox_ids = list(self._sandboxes.keys())
            warm_items = list(self._warm_pool.items())
            self._warm_pool.clear()

        # 停止空闲检查线程
        self._idle_checker_stop.set()
        if self._idle_checker_thread is not None and self._idle_checker_thread.is_alive():
            self._idle_checker_thread.join(timeout=5)
            logger.info("Stopped idle checker thread")

        logger.info(f"Shutting down {len(sandbox_ids)} active + {len(warm_items)} warm-pool sandbox(es)")

        for sandbox_id in sandbox_ids:
            try:
                self.destroy(sandbox_id)
            except Exception as e:
                logger.error(f"Failed to destroy sandbox {sandbox_id} during shutdown: {e}")

        for sandbox_id, (info, _) in warm_items:
            try:
                self._backend.destroy(info)
                logger.info(f"Destroyed warm-pool sandbox {sandbox_id} during shutdown")
            except Exception as e:
                logger.error(f"Failed to destroy warm-pool sandbox {sandbox_id} during shutdown: {e}")
