"""本地文件系统沙箱提供者 —— 管理每线程的 LocalSandbox 实例。

本模块实现了 :class:`LocalSandboxProvider`，它是
:class:`~deerflow.sandbox.sandbox_provider.SandboxProvider` 的本地文件系统实现。

核心设计
~~~~~~~~

每线程沙箱
^^^^^^^^^^
每个对话线程（thread）拥有独立的 :class:`LocalSandbox` 实例，其路径映射
将虚拟路径 ``/mnt/user-data/`` 指向该线程专属的宿主目录：

::

    /mnt/user-data/ → {base_dir}/users/{user_id}/threads/{thread_id}/user-data/
    /mnt/user-data/workspace/ → {base_dir}/users/{user_id}/threads/{thread_id}/user-data/workspace/
    /mnt/user-data/uploads/   → {base_dir}/users/{user_id}/threads/{thread_id}/user-data/uploads/
    /mnt/user-data/outputs/   → {base_dir}/users/{user_id}/threads/{thread_id}/user-data/outputs/
    /mnt/acp-workspace        → {base_dir}/users/{user_id}/threads/{thread_id}/acp-workspace/

这确保了不同线程之间的数据完全隔离。

静态映射
^^^^^^^^
除了每线程的动态映射外，还有一组全局共享的静态映射：

- ``/mnt/skills`` → 技能目录（只读）
- 自定义挂载（来自 config.yaml 的 sandbox.mounts 配置）

LRU 缓存
^^^^^^^^
为了避免在线程数无上限的长期运行进程中出现内存泄漏，使用 LRU（最近最少使用）
缓存来管理线程沙箱实例。默认上限为 256 个实例
（:data:`DEFAULT_MAX_CACHED_THREAD_SANDBOXES`）。

当缓存满时，最久未使用的线程沙箱会被淘汰。该线程下次 acquire 时会重建
新的沙箱实例（仅丢失 ``_agent_written_paths`` 反向解析提示，影响有限）。

线程安全
^^^^^^^^
所有缓存状态的变更（acquire、get、reset）都通过 ``self._lock`` 串行化，
确保多线程并发调用的安全性。

沙箱复用
^^^^^^^^
``release()`` 方法为空操作 —— LocalSandbox 没有需要释放的资源。缓存的实例
保留在 LRU 缓存中，使得 ``_agent_written_paths`` 在多轮对话间保持有效。
真正的清理通过 LRU 淘汰、显式 ``reset()`` 或 ``shutdown()`` 触发。
"""

import logging
import threading
from collections import OrderedDict
from pathlib import Path

from deerflow.sandbox.local.local_sandbox import LocalSandbox, PathMapping
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider

logger = logging.getLogger(__name__)

# 模块级别名，保持与旧代码/测试的向后兼容性。
# 旧代码可能直接访问 ``local_sandbox_provider._singleton``，
# 新代码应使用 Provider 实例属性（``_generic_sandbox`` / ``_thread_sandboxes``）。
_singleton: LocalSandbox | None = None

# 虚拟路径前缀常量：每线程映射中必须保留的路径前缀。
# 自定义挂载（config.yaml）不能与这些前缀冲突。
_USER_DATA_VIRTUAL_PREFIX = "/mnt/user-data"
_ACP_WORKSPACE_VIRTUAL_PREFIX = "/mnt/acp-workspace"

# 每线程 LocalSandbox 实例的 LRU 缓存默认上限。
# 每个缓存实例开销很小（一个 Python 对象，包含 PathMapping 列表和
# agent-written 路径集合），但在长期运行的 Gateway 中，线程数可能无上限增长。
# 超过上限时淘汰最久未使用的条目；被淘汰线程下次 acquire 时重建沙箱，
# 代价仅是丢失 ``_agent_written_paths``（read_file 回退到无反向解析行为，
# 与全新运行时一致）。
DEFAULT_MAX_CACHED_THREAD_SANDBOXES = 256


class LocalSandboxProvider(SandboxProvider):
    """基于本地文件系统的沙箱提供者，支持每线程路径隔离。

    早期版本的 Provider 返回一个进程级的 ``LocalSandbox`` 单例（id 为 ``"local"``），
    无法满足 ``/mnt/user-data/...`` 每线程隔离的契约。

    当前版本为每个 ``thread_id`` 生成独立的 ``LocalSandbox`` 实例，其
    ``path_mappings`` 包含线程专属的 ``/mnt/user-data/{workspace,uploads,outputs}``
    和 ``/mnt/acp-workspace`` 映射，与 :class:`AioSandboxProvider` 的 Docker
    bind-mount 行为一致。

    向后兼容：``acquire(None)`` 仍然返回 id 为 ``"local"`` 的通用单例，
    用于无线程上下文的调用者（如旧版测试、脚本）。

    线程安全：``acquire``、``get``、``reset`` 可能被多线程并发调用
    （Gateway 工具分发、子 Agent 工作池、后台内存更新器等），
    所有缓存状态变更通过 ``self._lock`` 串行化。

    内存控制：``_thread_sandboxes`` 是 LRU 缓存，上限为
    ``max_cached_threads``（默认 :data:`DEFAULT_MAX_CACHED_THREAD_SANDBOXES`）。
    超过上限时淘汰最久未使用的条目。
    """

    # 标识此 Provider 使用每线程的数据挂载
    uses_thread_data_mounts = True
    needs_upload_permission_adjustment = False

    def __init__(self, max_cached_threads: int = DEFAULT_MAX_CACHED_THREAD_SANDBOXES):
        """初始化本地沙箱提供者。

        在初始化时即创建静态路径映射（技能目录、自定义挂载），
        这些映射在所有沙箱实例间共享。每线程的动态映射在 ``acquire`` 时创建。

        Args:
            max_cached_threads: 每线程沙箱的 LRU 缓存上限。
                超过时淘汰最久未使用的条目。
        """
        # 静态路径映射（技能目录 + 自定义挂载），所有沙箱共享
        self._path_mappings = self._setup_path_mappings()
        # 通用沙箱单例（用于无线程上下文的场景）
        self._generic_sandbox: LocalSandbox | None = None
        # 每线程沙箱的 LRU 缓存：thread_id → LocalSandbox
        self._thread_sandboxes: OrderedDict[str, LocalSandbox] = OrderedDict()
        # LRU 缓存上限
        self._max_cached_threads = max_cached_threads
        # 保护缓存状态变更的全局互斥锁
        self._lock = threading.Lock()

    def _setup_path_mappings(self) -> list[PathMapping]:
        """创建静态路径映射，由所有沙箱实例共享。

        静态映射包括：
        1. 技能目录映射（``/mnt/skills`` → 本地技能目录，只读）
        2. config.yaml 中定义的自定义挂载（sandbox.mounts）

        每线程的 ``/mnt/user-data/...`` 和 ``/mnt/acp-workspace`` 映射
        在 :meth:`acquire` 中动态添加，因为它们依赖 thread_id 和 user_id。

        Returns:
            静态 PathMapping 列表。
        """
        mappings: list[PathMapping] = []

        # 映射技能目录（容器路径 → 本地路径）
        try:
            from deerflow.config import get_app_config

            config = get_app_config()
            skills_path = config.skills.get_skills_path()
            container_path = config.skills.container_path

            # 仅在技能目录存在时添加映射
            if skills_path.exists():
                mappings.append(
                    PathMapping(
                        container_path=container_path,
                        local_path=str(skills_path),
                        read_only=True,  # 技能目录始终只读
                    )
                )

            # 映射 config.yaml 中的自定义挂载
            _RESERVED_CONTAINER_PREFIXES = [
                container_path,
                _ACP_WORKSPACE_VIRTUAL_PREFIX,
                _USER_DATA_VIRTUAL_PREFIX,
            ]
            sandbox_config = config.sandbox
            if sandbox_config and sandbox_config.mounts:
                for mount in sandbox_config.mounts:
                    host_path = Path(mount.host_path)
                    container_path = mount.container_path.rstrip("/") or "/"

                    # 验证：host_path 必须是绝对路径
                    if not host_path.is_absolute():
                        logger.warning(
                            "Mount host_path must be absolute, skipping: %s -> %s",
                            mount.host_path,
                            mount.container_path,
                        )
                        continue

                    # 验证：container_path 必须以 / 开头
                    if not container_path.startswith("/"):
                        logger.warning(
                            "Mount container_path must be absolute, skipping: %s -> %s",
                            mount.host_path,
                            mount.container_path,
                        )
                        continue

                    # 验证：container_path 不能与保留前缀冲突
                    if any(container_path == p or container_path.startswith(p + "/") for p in _RESERVED_CONTAINER_PREFIXES):
                        logger.warning(
                            "Mount container_path conflicts with reserved prefix, skipping: %s",
                            mount.container_path,
                        )
                        continue
                    # 确保宿主机路径存在后才添加映射
                    if host_path.exists():
                        mappings.append(
                            PathMapping(
                                container_path=container_path,
                                local_path=str(host_path.resolve()),
                                read_only=mount.read_only,
                            )
                        )
                    else:
                        logger.warning(
                            "Mount host_path does not exist, skipping: %s -> %s",
                            mount.host_path,
                            mount.container_path,
                        )
        except Exception as e:
            # 配置加载失败时记录警告但不中断初始化
            logger.warning("Could not setup path mappings: %s", e, exc_info=True)

        return mappings

    @staticmethod
    def _build_thread_path_mappings(thread_id: str) -> list[PathMapping]:
        """构建每线程的路径映射（/mnt/user-data 和 /mnt/acp-workspace）。

        通过 :func:`get_effective_user_id` 解析用户 ID，确保宿主机目录存在后
        创建映射。目录结构与 :class:`AioSandboxProvider` 的 Docker bind-mount 保持一致。

        Args:
            thread_id: 线程标识符。

        Returns:
            每线程的 PathMapping 列表。
        """
        from deerflow.config.paths import get_paths
        from deerflow.runtime.user_context import get_effective_user_id

        paths = get_paths()
        user_id = get_effective_user_id()
        # 确保线程目录结构存在（workspace、uploads、outputs 等）
        paths.ensure_thread_dirs(thread_id, user_id=user_id)

        return [
            # 聚合父目录映射，使得 ``ls /mnt/user-data`` 等父级操作行为与
            # AIO 容器内一致（父目录是真实的且包含三个子目录）。
            # 更长的子路径映射（如 /mnt/user-data/workspace/...）仍然会
            # 在 _find_path_mapping 中按 container_path 长度优先匹配。
            PathMapping(
                container_path=_USER_DATA_VIRTUAL_PREFIX,
                local_path=str(paths.sandbox_user_data_dir(thread_id, user_id=user_id)),
                read_only=False,
            ),
            PathMapping(
                container_path=f"{_USER_DATA_VIRTUAL_PREFIX}/workspace",
                local_path=str(paths.sandbox_work_dir(thread_id, user_id=user_id)),
                read_only=False,
            ),
            PathMapping(
                container_path=f"{_USER_DATA_VIRTUAL_PREFIX}/uploads",
                local_path=str(paths.sandbox_uploads_dir(thread_id, user_id=user_id)),
                read_only=False,
            ),
            PathMapping(
                container_path=f"{_USER_DATA_VIRTUAL_PREFIX}/outputs",
                local_path=str(paths.sandbox_outputs_dir(thread_id, user_id=user_id)),
                read_only=False,
            ),
            PathMapping(
                container_path=_ACP_WORKSPACE_VIRTUAL_PREFIX,
                local_path=str(paths.acp_workspace_dir(thread_id, user_id=user_id)),
                read_only=False,
            ),
        ]

    def acquire(self, thread_id: str | None = None) -> str:
        """获取沙箱 ID，根据 thread_id 返回通用单例或线程专属沙箱。

        行为：
        - ``thread_id=None``：返回通用沙箱单例（id 为 ``"local"``），
          用于无线程上下文的调用者（旧版测试、脚本）。
        - ``thread_id="abc"``：返回线程专属沙箱（id 为 ``"local:abc"``），
          其路径映射将 ``/mnt/user-data/...`` 指向该线程的宿主目录。

        线程安全：缓存查询 + 插入在 ``self._lock`` 保护下执行，
        确保同一 thread_id 的并发 acquire 始终返回同一实例。

        Args:
            thread_id: 线程标识符。None 表示请求通用沙箱。

        Returns:
            沙箱实例的唯一 ID。
        """
        global _singleton

        if thread_id is None:
            # 无线程上下文：返回通用单例
            with self._lock:
                if self._generic_sandbox is None:
                    self._generic_sandbox = LocalSandbox("local", path_mappings=list(self._path_mappings))
                    _singleton = self._generic_sandbox
                return self._generic_sandbox.id

        # 快速路径：在锁内检查缓存
        with self._lock:
            cached = self._thread_sandboxes.get(thread_id)
            if cached is not None:
                # 标记为最近使用，使频繁访问的线程不被淘汰
                self._thread_sandboxes.move_to_end(thread_id)
                return cached.id

        # ``_build_thread_path_mappings`` 涉及文件系统操作（ensure_thread_dirs），
        # 在锁外执行以避免阻塞其他线程。
        new_mappings = list(self._path_mappings) + self._build_thread_path_mappings(thread_id)

        with self._lock:
            # 锁外 I/O 完成后再次检查：另一个调用者可能在我们计算映射时
            # 已经填充了缓存。
            cached = self._thread_sandboxes.get(thread_id)
            if cached is None:
                cached = LocalSandbox(f"local:{thread_id}", path_mappings=new_mappings)
                self._thread_sandboxes[thread_id] = cached
                # 检查是否超过 LRU 上限并淘汰
                self._evict_until_within_cap_locked()
            else:
                self._thread_sandboxes.move_to_end(thread_id)
            return cached.id

    def _evict_until_within_cap_locked(self) -> None:
        """LRU 淘汰：在超过缓存上限时移除最久未使用的线程沙箱。

        调用者必须持有 ``self._lock``。

        使用 OrderedDict 的 FIFO 语义（popitem(last=False) 移除最早插入的条目），
        配合 move_to_end() 实现经典的 LRU 效果。
        """
        while len(self._thread_sandboxes) > self._max_cached_threads:
            evicted_thread_id, _ = self._thread_sandboxes.popitem(last=False)
            logger.info(
                "Evicting LocalSandbox cache entry for thread %s (cap=%d)",
                evicted_thread_id,
                self._max_cached_threads,
            )

    def get(self, sandbox_id: str) -> Sandbox | None:
        """根据 ID 获取沙箱实例。

        支持两种 ID 格式：
        - ``"local"``：返回通用沙箱单例
        - ``"local:{thread_id}"``：返回对应线程的沙箱

        通过 ``get`` 访问的线程沙箱也会被提升到 LRU 最近位置，
        确保活跃线程在高负载下不被淘汰。

        Args:
            sandbox_id: 沙箱实例 ID。

        Returns:
            对应的 :class:`LocalSandbox` 实例，不存在则返回 None。
        """
        if sandbox_id == "local":
            # 通用沙箱：如果尚未创建则先 acquire
            with self._lock:
                generic = self._generic_sandbox
            if generic is None:
                self.acquire()
                with self._lock:
                    return self._generic_sandbox
            return generic
        if isinstance(sandbox_id, str) and sandbox_id.startswith("local:"):
            # 线程沙箱：从 LRU 缓存中查找
            thread_id = sandbox_id[len("local:") :]
            with self._lock:
                cached = self._thread_sandboxes.get(thread_id)
                if cached is not None:
                    # 通过 ``get`` 访问也提升 LRU 位置，
                    # 确保活跃线程（频繁执行工具调用的线程）不被淘汰。
                    self._thread_sandboxes.move_to_end(thread_id)
                return cached
        return None

    def release(self, sandbox_id: str) -> None:
        """释放沙箱实例（空操作）。

        LocalSandbox 没有需要释放的外部资源（如容器进程），因此 release
        不做任何操作，将实例保留在 LRU 缓存中。这使得 ``_agent_written_paths``
        在多轮对话间保持有效，支持 read_file 的反向路径解析。

        真正的清理通过以下路径触发：
        - LRU 淘汰（acquire 时超出上限）
        - 显式 ``reset()`` / ``shutdown()``

        注意：SandboxMiddleware 有意不调用此方法，以支持沙箱跨轮次复用。
        """
        pass

    def reset(self) -> None:
        """清除所有缓存的 LocalSandbox 实例。

        ``reset_sandbox_provider()`` 调用此方法，确保配置和挂载变更在下次
        ``acquire()`` 时生效。同时重置模块级 ``_singleton`` 别名，
        使旧代码/测试也能看到新的状态。
        """
        global _singleton
        with self._lock:
            self._generic_sandbox = None
            self._thread_sandboxes.clear()
            _singleton = None

    def shutdown(self) -> None:
        """关闭提供者。

        LocalSandboxProvider 没有额外资源需要释放（仅缓存 LocalSandbox 实例），
        因此 shutdown 与 reset 使用相同的清理路径。
        """
        self.reset()
