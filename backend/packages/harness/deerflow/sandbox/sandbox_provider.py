"""SandboxProvider 抽象基类与全局单例管理。

本模块定义了沙箱提供者（SandboxProvider）的抽象接口和全局单例管理机制。

SandboxProvider 是沙箱的**工厂与生命周期管理器**，负责：

- **acquire**：创建或获取一个沙箱实例，返回其 ID
- **get**：根据 ID 查找已有的沙箱实例
- **release**：释放不再使用的沙箱实例
- **reset**：清除缓存状态（用于配置变更后重新初始化）
- **shutdown**：关闭提供者并释放所有资源

单例模式
~~~~~~~~
本模块通过全局变量 ``_default_sandbox_provider`` 和以下函数实现单例模式：

- :func:`get_sandbox_provider` — 获取（或首次创建）全局单例
- :func:`reset_sandbox_provider` — 重置单例（不执行关闭操作）
- :func:`shutdown_sandbox_provider` — 关闭并重置单例
- :func:`set_sandbox_provider` — 注入自定义提供者（用于测试）

提供者类型通过配置文件 ``config.yaml`` 中的 ``sandbox.use`` 字段指定，
使用 ``resolve_class`` 动态加载。

Provider 实现一览
~~~~~~~~~~~~~~~~~~
- :class:`LocalSandboxProvider` — 本地文件系统沙箱，每个线程独立的路径映射
- ``AioSandboxProvider`` — Docker 容器沙箱，提供完整的进程隔离

线程安全
~~~~~~~~
所有 Provider 实现必须保证 ``acquire``、``get``、``release`` 方法是线程安全的，
因为 Gateway 工具分发、子 Agent 工作池、后台内存更新器等组件可能在不同的
线程中并发调用这些方法。
"""

from abc import ABC, abstractmethod

from deerflow.config import get_app_config
from deerflow.reflection import resolve_class
from deerflow.sandbox.sandbox import Sandbox


class SandboxProvider(ABC):
    """沙箱提供者的抽象基类。

    定义了沙箱实例的创建、获取和释放的标准接口。每个具体的沙箱后端
    （如本地文件系统、Docker 容器）都需要实现此基类。

    Attributes:
        uses_thread_data_mounts: 标识此 Provider 是否使用每线程的数据挂载。
            LocalSandboxProvider 将此设为 True，因为它为每个线程创建独立的
            路径映射（/mnt/user-data/ → 每线程宿主目录）。
    """

    uses_thread_data_mounts: bool = False

    @abstractmethod
    def acquire(self, thread_id: str | None = None) -> str:
        """创建或获取一个沙箱实例，返回其 ID。

        根据 thread_id 创建或复用沙箱实例。如果 thread_id 为 None，
        返回通用的沙箱实例（用于无线程上下文的场景，如测试或脚本）。

        Args:
            thread_id: 线程标识符。如果提供，返回该线程专属的沙箱实例。

        Returns:
            沙箱实例的唯一标识符。
        """
        pass

    @abstractmethod
    def get(self, sandbox_id: str) -> Sandbox | None:
        """根据 ID 获取已有的沙箱实例。

        Args:
            sandbox_id: 要获取的沙箱实例 ID。

        Returns:
            对应的 :class:`Sandbox` 实例，如果不存在则返回 None。
        """
        pass

    @abstractmethod
    def release(self, sandbox_id: str) -> None:
        """释放不再使用的沙箱实例。

        释放后该 sandbox_id 将不再有效。具体行为取决于实现：
        - 本地沙箱：可能保留缓存以支持跨轮次复用
        - 容器沙箱：停止并删除容器

        Args:
            sandbox_id: 要释放的沙箱实例 ID。
        """
        pass

    def reset(self) -> None:
        """清除在 Provider 实例替换后仍存活的缓存状态。

        子类可重写此方法以清除模块级别的状态。例如 LocalSandboxProvider
        使用此方法清除缓存的 LocalSandbox 单例，确保配置和挂载变更在下次
        acquire() 时生效。

        注意：如果有活跃的沙箱实例，它们将成为孤儿状态。
        使用 :meth:`shutdown` 进行完整的清理。
        """
        pass


# 全局 Provider 单例实例。首次调用 get_sandbox_provider() 时根据配置创建。
_default_sandbox_provider: SandboxProvider | None = None


def get_sandbox_provider(**kwargs) -> SandboxProvider:
    """获取沙箱提供者的全局单例。

    首次调用时根据 ``config.yaml`` 中的 ``sandbox.use`` 配置动态加载并实例化
    Provider 类。后续调用直接返回缓存的实例。

    使用 ``reset_sandbox_provider()`` 清除缓存，或使用
    ``shutdown_sandbox_provider()`` 执行完整关闭并清除。

    Args:
        **kwargs: 传递给 Provider 构造函数的额外参数（仅在首次创建时使用）。

    Returns:
        沙箱提供者实例。
    """
    global _default_sandbox_provider
    if _default_sandbox_provider is None:
        config = get_app_config()
        # 通过配置中的类路径字符串动态加载 Provider 类
        cls = resolve_class(config.sandbox.use, SandboxProvider)
        _default_sandbox_provider = cls(**kwargs)
    return _default_sandbox_provider


def reset_sandbox_provider() -> None:
    """重置沙箱提供者单例（不执行关闭操作）。

    清除缓存的实例，下次调用 ``get_sandbox_provider()`` 将创建新实例。
    适用于测试场景或配置切换。

    Provider 可以重写 ``reset()`` 方法来清除跨实例保持的模块级状态。
    例如 LocalSandboxProvider 的缓存 LocalSandbox 单例。如果不重置，
    配置/挂载变更在下次 acquire() 时不会生效。

    注意：如果有活跃的沙箱实例，它们将成为孤儿状态。
    如需完整清理，请使用 ``shutdown_sandbox_provider()``。
    """
    global _default_sandbox_provider
    if _default_sandbox_provider is not None:
        # 调用 Provider 的 reset 方法清除其内部缓存
        _default_sandbox_provider.reset()
        _default_sandbox_provider = None


def shutdown_sandbox_provider() -> None:
    """关闭并重置沙箱提供者单例。

    在清除单例之前，先调用 Provider 的 ``shutdown()`` 方法（如果存在），
    确保所有沙箱实例被正确释放。适用于应用程序关闭或需要完全重置沙箱系统的场景。
    """
    global _default_sandbox_provider
    if _default_sandbox_provider is not None:
        # 如果 Provider 实现了 shutdown 方法，先执行完整的关闭流程
        if hasattr(_default_sandbox_provider, "shutdown"):
            _default_sandbox_provider.shutdown()
        _default_sandbox_provider = None


def set_sandbox_provider(provider: SandboxProvider) -> None:
    """设置自定义的沙箱提供者实例。

    允许注入自定义或模拟的 Provider 实例，主要用于测试目的。
    设置后将替代当前的单例实例，直到下次 reset 或 shutdown。

    Args:
        provider: 要使用的 SandboxProvider 实例。
    """
    global _default_sandbox_provider
    _default_sandbox_provider = provider
