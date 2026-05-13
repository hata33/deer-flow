"""沙箱提供者抽象接口和全局单例管理。

通过配置驱动的反射机制（resolve_class）加载沙箱实现，
支持 acquire/get/release 生命周期管理。
"""

from abc import ABC, abstractmethod

from deerflow.config import get_app_config
from deerflow.reflection import resolve_class
from deerflow.sandbox.sandbox import Sandbox


class SandboxProvider(ABC):
    """沙箱提供者抽象基类，定义沙箱的生命周期管理接口。"""

    @abstractmethod
    def acquire(self, thread_id: str | None = None) -> str:
        """获取一个沙箱环境并返回其 ID。

        Args:
            thread_id: 关联的线程 ID，用于路径隔离。

        Returns:
            沙箱 ID。
        """
        pass

    @abstractmethod
    def get(self, sandbox_id: str) -> Sandbox | None:
        """根据 ID 获取沙箱实例。

        Args:
            sandbox_id: 沙箱 ID。

        Returns:
            沙箱实例，不存在时返回 None。
        """
        pass

    @abstractmethod
    def release(self, sandbox_id: str) -> None:
        """释放沙箱环境。

        Args:
            sandbox_id: 待释放的沙箱 ID。
        """
        pass


_default_sandbox_provider: SandboxProvider | None = None


def get_sandbox_provider(**kwargs) -> SandboxProvider:
    """获取沙箱提供者全局单例。

    首次调用时通过 resolve_class 从配置加载实现类并实例化，
    后续调用返回缓存实例。

    Returns:
        沙箱提供者实例。
    """
    global _default_sandbox_provider
    if _default_sandbox_provider is None:
        config = get_app_config()
        cls = resolve_class(config.sandbox.use, SandboxProvider)
        _default_sandbox_provider = cls(**kwargs)
    return _default_sandbox_provider


def reset_sandbox_provider() -> None:
    """重置沙箱提供者单例（不调用 shutdown，用于测试或配置切换）。"""
    global _default_sandbox_provider
    _default_sandbox_provider = None


def shutdown_sandbox_provider() -> None:
    """关闭并重置沙箱提供者（释放所有沙箱后清空单例）。"""
    global _default_sandbox_provider
    if _default_sandbox_provider is not None:
        if hasattr(_default_sandbox_provider, "shutdown"):
            _default_sandbox_provider.shutdown()
        _default_sandbox_provider = None


def set_sandbox_provider(provider: SandboxProvider) -> None:
    """注入自定义沙箱提供者（用于测试）。

    Args:
        provider: 自定义的 SandboxProvider 实例。
    """
    global _default_sandbox_provider
    _default_sandbox_provider = provider
