"""本地沙箱提供者。

单例模式管理 LocalSandbox 实例，从配置中读取路径映射。
同一进程中所有线程共享同一个沙箱实例。
"""

import logging

from deerflow.sandbox.local.local_sandbox import LocalSandbox
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider

logger = logging.getLogger(__name__)

# 全局单例：所有线程共享同一个本地沙箱实例
_singleton: LocalSandbox | None = None


class LocalSandboxProvider(SandboxProvider):
    """本地沙箱提供者：单例模式，路径映射从配置加载。"""

    def __init__(self):
        """初始化本地沙箱提供者，从应用配置中构建路径映射。"""
        self._path_mappings = self._setup_path_mappings()

    def _setup_path_mappings(self) -> dict[str, str]:
        """从配置中构建容器路径到本地路径的映射（如 skills 目录）。"""
        mappings = {}

        # 将 skills 容器路径映射到本地 skills 目录
        try:
            from deerflow.config import get_app_config

            config = get_app_config()
            skills_path = config.skills.get_skills_path()
            container_path = config.skills.container_path

            # 仅在 skills 目录实际存在时添加映射
            if skills_path.exists():
                mappings[container_path] = str(skills_path)
        except Exception as e:
            # 配置加载失败时记录警告但不中断初始化
            logger.warning("Could not setup skills path mapping: %s", e, exc_info=True)

        return mappings

    def acquire(self, thread_id: str | None = None) -> str:
        """获取沙箱 ID（懒初始化单例）。"""
        global _singleton
        if _singleton is None:
            _singleton = LocalSandbox("local", path_mappings=self._path_mappings)
        return _singleton.id

    def get(self, sandbox_id: str) -> Sandbox | None:
        """根据 ID 获取沙箱实例（仅支持 "local"）。"""
        if sandbox_id == "local":
            if _singleton is None:
                self.acquire()
            return _singleton
        return None

    def release(self, sandbox_id: str) -> None:
        """释放沙箱（本地沙箱为单例模式，无需清理）。

        注意：SandboxMiddleware 有意不调用此方法，以允许沙箱在同一线程的多轮对话中复用。
        Docker 沙箱（AioSandboxProvider）通过 shutdown() 方法统一清理。
        """
        pass
