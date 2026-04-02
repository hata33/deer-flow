"""
沙箱抽象接口
"""
from abc import ABC, abstractmethod
from typing import Self


class Sandbox(ABC):
    """
    沙箱抽象基类

    定义了隔离执行环境的接口
    """

    _id: str

    def __init__(self, id: str):
        self._id = id

    @property
    def id(self) -> str:
        """沙箱 ID"""
        return self._id

    @abstractmethod
    def execute_command(self, command: str) -> str:
        """
        在沙箱中执行命令

        Args:
            command: 要执行的命令

        Returns:
            命令输出（stdout + stderr）
        """
        pass

    @abstractmethod
    def read_file(self, path: str) -> str:
        """
        读取文件内容

        Args:
            path: 文件路径

        Returns:
            文件内容
        """
        pass

    @abstractmethod
    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """
        写入文件

        Args:
            path: 文件路径
            content: 文件内容
            append: 是否追加模式
        """
        pass

    @abstractmethod
    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        """
        列出目录内容

        Args:
            path: 目录路径
            max_depth: 最大深度

        Returns:
            文件和目录列表
        """
        pass


class SandboxProvider(ABC):
    """
    沙箱提供者抽象类

    管理沙箱的生命周期
    """

    @abstractmethod
    async def acquire(self, sandbox_id: str | None = None) -> Sandbox:
        """
        获取或创建沙箱

        Args:
            sandbox_id: 沙箱 ID，如果为 None 则创建新的

        Returns:
            沙箱实例
        """
        pass

    @abstractmethod
    async def release(self, sandbox_id: str) -> None:
        """
        释放沙箱

        Args:
            sandbox_id: 沙箱 ID
        """
        pass

    @abstractmethod
    def get(self, sandbox_id: str) -> Sandbox | None:
        """
        获取现有沙箱

        Args:
            sandbox_id: 沙箱 ID

        Returns:
            沙箱实例，如果不存在则返回 None
        """
        pass
