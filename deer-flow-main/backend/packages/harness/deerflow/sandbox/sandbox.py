"""沙箱抽象接口。

定义沙箱环境的标准操作接口：命令执行、文件读写、目录列表。
所有沙箱实现（本地、Docker）都必须实现此接口。
"""

from abc import ABC, abstractmethod


class Sandbox(ABC):
    """沙箱环境抽象基类，定义命令执行和文件操作的统一接口。"""

    _id: str

    def __init__(self, id: str):
        self._id = id

    @property
    def id(self) -> str:
        """沙箱唯一标识符。"""
        return self._id

    @abstractmethod
    def execute_command(self, command: str) -> str:
        """在沙箱中执行 bash 命令。

        Args:
            command: 待执行的命令。

        Returns:
            命令的标准输出或错误输出。
        """
        pass

    @abstractmethod
    def read_file(self, path: str) -> str:
        """读取沙箱中的文件内容。

        Args:
            path: 文件的绝对路径。

        Returns:
            文件内容字符串。
        """
        pass

    @abstractmethod
    def list_dir(self, path: str, max_depth=2) -> list[str]:
        """列出沙箱中目录的内容（树形格式）。

        Args:
            path: 目录的绝对路径。
            max_depth: 最大遍历深度，默认 2。

        Returns:
            目录内容字符串列表。
        """
        pass

    @abstractmethod
    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """向沙箱中的文件写入文本内容。

        Args:
            path: 文件的绝对路径。
            content: 待写入的文本内容。
            append: 是否追加模式，False 时创建或覆盖。
        """
        pass

    @abstractmethod
    def update_file(self, path: str, content: bytes) -> None:
        """用二进制内容更新沙箱中的文件。

        Args:
            path: 文件的绝对路径。
            content: 待写入的二进制内容。
        """
        pass
